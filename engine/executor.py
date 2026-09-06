"""Order placement - the only place in the bot that can create real orders.

Paper mode still runs the entire pipeline, including the schema binding, and logs
the exact payload that *would* have been sent.  That makes the dry run a genuine
rehearsal rather than a different code path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from config import Config
from mcp_client.ctrader import CTraderGateway, OrderRequest
from risk.sizing import SizingResult
from strategy.models import TradeSetup
from utils.logging import get_logger
from utils.prices import round_price
from utils.telegram import TelegramNotifier, esc

log = get_logger("engine.executor")


@dataclass
class ExecutionRecord:
    submitted: bool
    simulated: bool
    setup: TradeSetup
    sizing: SizingResult
    label: str
    payloads: list[dict[str, Any]] = field(default_factory=list)
    responses: list[Any] = field(default_factory=list)
    error: str = ""

    @property
    def payload(self) -> dict[str, Any]:
        """The first rung - convenient when there is only one."""
        return self.payloads[0] if self.payloads else {}


class OrderExecutor:
    def __init__(self, cfg: Config, gateway: CTraderGateway,
                 notifier: TelegramNotifier) -> None:
        self._cfg = cfg
        self._gateway = gateway
        self._notifier = notifier

    def ladder_prices(self, setup: TradeSetup, layers: int) -> list[float]:
        """Entry prices for each rung, spread across the fair value gap.

        Rung 1 sits at the configured entry edge (the price reached first), and
        the last rung at the far edge, so a deeper retracement fills more of the
        position at a better price. With one layer, or without an FVG to span,
        this is just the single configured entry.
        """
        if layers <= 1 or setup.fvg is None:
            return [setup.entry]
        start, end = setup.entry, setup.fvg.distal
        step = (end - start) / (layers - 1)
        point = self._gateway.point_size
        return [round_price(start + step * i, point) for i in range(layers)]

    def build_requests(self, setup: TradeSetup, sizing: SizingResult,
                       label: str) -> list[OrderRequest]:
        """One OrderRequest per rung. Stop and target are shared: the ladder
        changes where we get in, not where the idea is invalidated."""
        expiry = datetime.now(tz=timezone.utc) + timedelta(
            minutes=self._cfg.order_expiry_minutes)
        prices = self.ladder_prices(setup, sizing.layers)
        volume = sizing.lots_per_layer or sizing.lots
        multi = len(prices) > 1
        return [
            OrderRequest(
                side=setup.direction.value,
                order_type="LIMIT",      # Silver Bullet entries are always limits
                volume_lots=volume,
                price=price,
                stop_loss=setup.stop_loss,
                take_profit=setup.take_profit,
                label=f"{label}-L{i + 1}" if multi else label,
                comment=f"ICT SB {setup.window}",
                expiry=expiry,
            )
            for i, price in enumerate(prices)
        ]

    def build_request(self, setup: TradeSetup, sizing: SizingResult,
                      label: str) -> OrderRequest:
        """Single-rung convenience used by the boot-time preflight."""
        return self.build_requests(setup, sizing, label)[0]

    async def execute(self, setup: TradeSetup, sizing: SizingResult,
                      label: str) -> ExecutionRecord:
        requests = self.build_requests(setup, sizing, label)

        # Bind every rung against the live schema first: in paper mode this
        # proves the orders *could* have been sent, and in live mode it fails
        # before any network call if a required field cannot be filled. Binding
        # all of them up front also avoids a half-placed ladder.
        payloads: list[dict[str, Any]] = []
        try:
            for request in requests:
                payloads.append(self._gateway.describe_order_payload(request))
        except Exception as exc:  # noqa: BLE001
            log.error("Could not build the create_order payload: %s", exc)
            await self._notifier.send(
                f"⚠️ <b>Order build failed</b>\n{esc(str(exc)[:400])}",
                dedup_key="order-build-failed",
            )
            return ExecutionRecord(False, False, setup, sizing, label, error=str(exc))

        if not self._cfg.is_live:
            for payload in payloads:
                log.info("[PAPER] Would submit create_order: %s", payload)
            await self._notify(setup, sizing, label, requests, simulated=True)
            return ExecutionRecord(False, True, setup, sizing, label, payloads=payloads)

        responses: list[Any] = []
        for index, request in enumerate(requests, start=1):
            try:
                responses.append(await self._gateway.create_order(request))
            except Exception as exc:  # noqa: BLE001
                log.error("create_order failed on rung %s/%s: %s", index, len(requests), exc)
                await self._notifier.send(
                    f"❌ <b>Order rejected</b> (rung {index}/{len(requests)})\n"
                    f"{esc(setup.direction.value)} {request.volume_lots} lots "
                    f"@ {request.price:.2f}\n<code>{esc(str(exc)[:400])}</code>",
                    dedup_key=f"order-error-{label}-{index}",
                )
                # Report what did get placed rather than pretending it all failed.
                return ExecutionRecord(bool(responses), False, setup, sizing, label,
                                       payloads=payloads, responses=responses,
                                       error=str(exc))

        log.info("Ladder accepted (%s rung(s)): %s", len(responses), responses)
        await self._notify(setup, sizing, label, requests, simulated=False)
        return ExecutionRecord(True, False, setup, sizing, label,
                               payloads=payloads, responses=responses)

    async def _notify(self, setup: TradeSetup, sizing: SizingResult, label: str,
                      requests: list[OrderRequest], *, simulated: bool) -> None:
        header = "📄 <b>PAPER ORDER</b>" if simulated else "🎯 <b>ORDER PLACED</b>"
        arrow = "🔻" if setup.direction.value == "SELL" else "🔺"
        point = self._gateway.point_size
        narrative = "\n".join(f"• {esc(line)}" for line in setup.narrative)

        if len(requests) > 1:
            rungs = "\n".join(
                f"   L{i}: {round(r.volume_lots, 8):g} @ {r.price:.2f}"
                for i, r in enumerate(requests, start=1)
            )
            entry_block = (f"<b>Entry ladder</b> {len(requests)} x {sizing.lots_per_layer} "
                           f"lots\n{rungs}\n")
        else:
            entry_block = f"<b>Entry</b> {setup.entry:.2f}\n"

        await self._notifier.send(
            f"{header}\n"
            f"{arrow} <b>{esc(setup.direction.value)} LIMIT "
            f"{esc(self._gateway.active_name)}</b> {round(sizing.lots, 8):g} lots total\n"
            f"{entry_block}"
            f"<b>SL</b> {setup.stop_loss:.2f}  ({setup.stop_distance / point:.0f}pt)\n"
            f"<b>TP</b> {setup.take_profit:.2f}  ({setup.risk_reward:.2f}R)\n"
            f"<b>Risk</b> {sizing.actual_risk:.2f} ({sizing.risk_pct:.2f}% of balance)"
            f"{' — fixed size' if sizing.fixed else ''}\n"
            f"<b>Window</b> {esc(setup.window)} | <code>{esc(label)}</code>\n\n"
            f"{narrative}"
        )
