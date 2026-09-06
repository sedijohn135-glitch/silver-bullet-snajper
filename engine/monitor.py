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


@dataclass(frozen=True)
class Closure:
    position_id: str
    pnl: float
    outcome: str
    pnl_known: bool          # False when no closing deal could be matched


@dataclass
class MonitorReport:
    positions: list[Position]
    pending: list[PendingOrder]
    deals: list[Deal]
    filled: list[Position]
    closed: list[Closure]


class TradeMonitor:
    #: How many polls to wait for a closing deal before giving up on the P&L.
    DEAL_LOOKUP_ATTEMPTS = 6

    def __init__(self, cfg: Config, gateway: CTraderGateway,
                 notifier: TelegramNotifier, state: BotState) -> None:
        self._cfg = cfg
        self._gateway = gateway
        self._notifier = notifier
        self._state = state
        self._tracked: dict[str, Position] = {}
        #: position id -> polls spent waiting for its closing deal to appear.
        self._awaiting_deals: dict[str, int] = {}
        #: False until the first poll has seen what was already open.
        self._synced = False

    async def poll(self, day_start: datetime) -> MonitorReport:
        """Refresh account state and emit notifications for any transitions."""
        positions = await self._gateway.get_positions()
        pending = await self._gateway.get_pending_orders()
        deals = await self._gateway.get_deals(day_start)

        if not self._synced:
            # First poll of this process. Railway containers are ephemeral, so
            # the local state file is gone after every redeploy and everything
            # already open would look newly filled. Adopt what exists silently -
            # a position that was open before this process started is not a fill
            # this process just saw.
            self._synced = True
            adopted = {p.position_id for p in positions if p.position_id}
            if adopted - self._state.known_position_ids:
                log.info("Adopting %d position(s) already open at start-up: %s",
                         len(adopted), sorted(adopted))
            self._state.known_position_ids |= adopted
            self._state.known_order_ids |= {o.order_id for o in pending if o.order_id}
            self._tracked = {p.position_id: p for p in positions}
            return MonitorReport(positions, pending, deals, [], [])

        filled = self._detect_fills(positions)
        closed = await self._detect_closures(positions, deals)

        self._state.known_position_ids = {p.position_id for p in positions}
        self._state.known_order_ids = {o.order_id for o in pending}
        self._tracked = {
            **{pid: pos for pid, pos in self._tracked.items() if pid in self._awaiting_deals},
            **{p.position_id: p for p in positions},
        }

        for position in filled:
            await self._notify_fill(position)
        for closure in closed:
            await self._notify_close(closure)

        return MonitorReport(positions, pending, deals, filled, closed)

    # -- transitions -------------------------------------------------------

    def _detect_fills(self, positions: Sequence[Position]) -> list[Position]:
        """Positions that appeared since the last poll, on the symbol we trade.

        Deliberately not gated on our order label: brokers do not reliably echo
        a pending order's label onto the position it becomes, and a fill that
        goes unannounced is the single worst thing this monitor can do. Anything
        new on the active symbol is reported; the message says whether it carries
        our label.
        """
        known = self._state.known_position_ids
        symbol_id = self._gateway.symbol.symbol_id if self._gateway.symbol else None
        fresh = []
        for position in positions:
            if not position.position_id or position.position_id in known:
                continue
            same_symbol = (
                symbol_id is not None and position.symbol_id == symbol_id
            ) or (
                position.symbol_name
                and position.symbol_name.upper() == self._gateway.active_name.upper()
            )
            if same_symbol or self._is_ours(position.label):
                fresh.append(position)
        return fresh

    async def _detect_closures(
        self, positions: Sequence[Position], deals: Sequence[Deal]
    ) -> list[Closure]:
        """Positions that vanished since the last poll, with their realised P&L.

        The closing deal reaches ``get_deals`` a little after the position
        disappears from ``get_positions``. Reporting immediately therefore finds
        no deal, sums to zero, and announces "+0.00" as though that were the
        result - and then never revisits it. So a closure with no matching deal
        is *deferred* for a few polls, and only reported as unknown if the deal
        never turns up.
        """
        current = {p.position_id for p in positions}
        vanished = [pid for pid in self._state.known_position_ids
                    if pid and pid not in current and pid not in self._state.reported_closures]
        for position_id in vanished:
            self._awaiting_deals.setdefault(position_id, 0)

        closures: list[Closure] = []
        for position_id, attempts in list(self._awaiting_deals.items()):
            related = [d for d in deals if d.position_id == position_id]
            attempts += 1                       # this poll is one lookup
            self._awaiting_deals[position_id] = attempts
            if not related and attempts < self.DEAL_LOOKUP_ATTEMPTS:
                log.debug("Closure of %s has no deal yet (lookup %s/%s); deferring",
                          position_id, attempts, self.DEAL_LOOKUP_ATTEMPTS)
                continue

            pnl = sum(d.net_profit for d in related)
            tracked = self._tracked.get(position_id)
            if related:
                outcome = self._classify(pnl, tracked, related)
            else:
                outcome = "CLOSED"
                log.warning("Position %s closed but no deal was found after %s lookups; "
                            "reporting without a P&L figure.",
                            position_id, attempts)
            self._awaiting_deals.pop(position_id, None)
            self._state.reported_closures.add(position_id)
            closures.append(Closure(position_id, pnl, outcome, pnl_known=bool(related)))
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

    async def _notify_close(self, closure: Closure) -> None:
        if not closure.pnl_known:
            # Never print "+0.00" for a figure we could not read - that reads as
            # a breakeven result rather than as missing information.
            await self._notifier.send(
                f"⚪ <b>{esc(closure.outcome)}</b>\n"
                f"Position <code>{esc(closure.position_id)}</code>\n"
                f"<b>Realised P&amp;L</b> not reported by the broker yet - "
                f"check the account history.",
                dedup_key=f"close-{closure.position_id}",
            )
            return
        icon = "🟢" if closure.pnl > 0 else ("🔴" if closure.pnl < 0 else "⚪")
        await self._notifier.send(
            f"{icon} <b>{esc(closure.outcome)}</b>\n"
            f"Position <code>{esc(closure.position_id)}</code>\n"
            f"<b>Realised P&amp;L</b> {closure.pnl:+.2f}",
            dedup_key=f"close-{closure.position_id}",
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
