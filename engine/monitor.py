"""Position and order lifecycle monitoring.

The bot places limit orders and then has to notice, on its own, what happened to
them: filled, cancelled, stopped out, or taken to target.  There is no push
channel here, so state is diffed between polls and closures are attributed by
reading the broker's deal history rather than guessing from price.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

from config import Config
from mcp_client.ctrader import CTraderGateway
from engine.state import BotState
from models import Deal, PendingOrder, Position
from utils.logging import get_logger
from utils.telegram import TelegramNotifier, esc

log = get_logger("engine.monitor")


@dataclass
class MonitorReport:
    positions: list[Position]
    pending: list[PendingOrder]
    deals: list[Deal]
    filled: list[Position]
    closed: list[tuple[str, float, str]]   # (position_id, pnl, outcome)


class TradeMonitor:
    def __init__(self, cfg: Config, gateway: CTraderGateway,
                 notifier: TelegramNotifier, state: BotState) -> None:
        self._cfg = cfg
        self._gateway = gateway
        self._notifier = notifier
        self._state = state
        self._tracked: dict[str, Position] = {}

    async def poll(self, day_start: datetime) -> MonitorReport:
        """Refresh account state and emit notifications for any transitions."""
        positions = await self._gateway.get_positions()
        pending = await self._gateway.get_pending_orders()
        deals = await self._gateway.get_deals(day_start)

        filled = self._detect_fills(positions)
        closed = await self._detect_closures(positions, deals)

        self._state.known_position_ids = {p.position_id for p in positions}
        self._state.known_order_ids = {o.order_id for o in pending}
        self._tracked = {p.position_id: p for p in positions}

        for position in filled:
            await self._notify_fill(position)
        for position_id, pnl, outcome in closed:
            await self._notify_close(position_id, pnl, outcome)

        return MonitorReport(positions, pending, deals, filled, closed)

    # -- transitions -------------------------------------------------------

    def _detect_fills(self, positions: Sequence[Position]) -> list[Position]:
        """Positions we have not seen before, that carry one of our labels."""
        known = self._state.known_position_ids
        return [
            p for p in positions
            if p.position_id and p.position_id not in known and self._is_ours(p.label)
        ]

    async def _detect_closures(
        self, positions: Sequence[Position], deals: Sequence[Deal]
    ) -> list[tuple[str, float, str]]:
        """Positions that vanished since the last poll, with their realised P&L."""
        current = {p.position_id for p in positions}
        vanished = [pid for pid in self._state.known_position_ids
                    if pid and pid not in current and pid not in self._state.reported_closures]

        closures: list[tuple[str, float, str]] = []
        for position_id in vanished:
            related = [d for d in deals if d.position_id == position_id]
            pnl = sum(d.net_profit for d in related)
            tracked = self._tracked.get(position_id)
            outcome = self._classify(pnl, tracked, related)
            self._state.reported_closures.add(position_id)
            closures.append((position_id, pnl, outcome))
        return closures

    def _classify(self, pnl: float, position: Optional[Position],
                  deals: Sequence[Deal]) -> str:
        """Label a closure as target, stop, or manual/other.

        Price is compared against the position's own SL/TP when we have them;
        otherwise the sign of realised P&L is the only honest signal available.
        """
        exit_price = next((d.price for d in reversed(deals) if d.price), None)
        if position and exit_price is not None:
            tolerance = self._gateway.point_size * 30
            if position.take_profit and abs(exit_price - position.take_profit) <= tolerance:
                return "TAKE PROFIT"
            if position.stop_loss and abs(exit_price - position.stop_loss) <= tolerance:
                return "STOP LOSS"
        if pnl > 0:
            return "CLOSED IN PROFIT"
        if pnl < 0:
            return "CLOSED IN LOSS"
        return "CLOSED"

    def _is_ours(self, label: str) -> bool:
        return bool(label) and label.startswith(self._cfg.order_label_prefix)

    # -- notifications -----------------------------------------------------

    async def _notify_fill(self, position: Position) -> None:
        symbol = position.symbol_name or self._gateway.active_name
        lines = [
            "✅ <b>ORDER FILLED</b>",
            f"{esc(position.side)} {position.volume} {esc(symbol)}"
            + (f" @ {position.entry_price:.2f}" if position.entry_price else ""),
        ]
        if position.stop_loss:
            lines.append(f"<b>SL</b> {position.stop_loss:.2f}")
        if position.take_profit:
            lines.append(f"<b>TP</b> {position.take_profit:.2f}")
        await self._notifier.send("\n".join(lines),
                                  dedup_key=f"fill-{position.position_id}")

    async def _notify_close(self, position_id: str, pnl: float, outcome: str) -> None:
        icon = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
        await self._notifier.send(
            f"{icon} <b>{esc(outcome)}</b>\n"
            f"Position <code>{esc(position_id)}</code>\n"
            f"<b>Realised P&amp;L</b> {pnl:+.2f}",
            dedup_key=f"close-{position_id}",
        )

    # -- housekeeping ------------------------------------------------------

    async def cancel_stale_orders(self, pending: Sequence[PendingOrder],
                                  *, window_label: Optional[str] = None) -> int:
        """Cancel unfilled Silver Bullet limits once their window has passed.

        An FVG entry that was not taken inside its window has lost its premise;
        leaving it resting means an unattended trade could trigger hours later on
        completely different conditions.
        """
        cancelled = 0
        for order in pending:
            if not self._is_ours(order.label):
                continue
            if window_label and window_label in (order.label or ""):
                continue   # belongs to the window that is still running
            try:
                await self._gateway.cancel_order(order.order_id)
                cancelled += 1
                log.info("Cancelled stale order %s (%s)", order.order_id, order.label)
                price = f" @ {order.price:.2f}" if order.price else ""
                await self._notifier.send(
                    f"🚫 <b>Order cancelled</b> (window closed)\n"
                    f"<code>{esc(order.label)}</code>{price}",
                    dedup_key=f"cancel-{order.order_id}",
                )
            except Exception as exc:  # noqa: BLE001 - one bad cancel must not stop the sweep
                log.warning("Could not cancel order %s: %s", order.order_id, exc)
        return cancelled
