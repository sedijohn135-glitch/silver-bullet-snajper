"""Order placement - the only place in the bot that can create real orders.

Paper mode still runs the entire pipeline, including the schema binding, and logs
the exact payload that *would* have been sent.  That makes the dry run a genuine
rehearsal rather than a different code path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from config import Config
from mcp_client.ctrader import CTraderGateway, OrderRequest
from risk.sizing import SizingResult
from strategy.models import TradeSetup
from utils.logging import get_logger
from utils.telegram import TelegramNotifier, esc

log = get_logger("engine.executor")


@dataclass
class ExecutionRecord:
    submitted: bool
    simulated: bool
    setup: TradeSetup
    sizing: SizingResult
    label: str
    payload: dict[str, Any]
    response: Any = None
    error: str = ""


class OrderExecutor:
    def __init__(self, cfg: Config, gateway: CTraderGateway,
                 notifier: TelegramNotifier) -> None:
        self._cfg = cfg
        self._gateway = gateway
        self._notifier = notifier

    def build_request(self, setup: TradeSetup, sizing: SizingResult,
                      label: str) -> OrderRequest:
        return OrderRequest(
            side=setup.direction.value,
            order_type="LIMIT",          # Silver Bullet entries are always limits
            volume_lots=sizing.lots,
            price=setup.entry,
            stop_loss=setup.stop_loss,
            take_profit=setup.take_profit,
            label=label,
            comment=f"ICT SB {setup.window}",
            expiry=datetime.now(tz=timezone.utc)
            + timedelta(minutes=self._cfg.order_expiry_minutes),
        )

    async def execute(self, setup: TradeSetup, sizing: SizingResult,
                      label: str) -> ExecutionRecord:
        request = self.build_request(setup, sizing, label)

        # Bind against the live schema first: in paper mode this proves the order
        # *could* have been sent, and in live mode it fails before any network call
        # if a required field cannot be filled.
        try:
            payload = self._gateway.describe_order_payload(request)
        except Exception as exc:  # noqa: BLE001
            log.error("Could not build the create_order payload: %s", exc)
            await self._notifier.send(
                f"⚠️ <b>Order build failed</b>\n{esc(str(exc)[:400])}",
                dedup_key="order-build-failed",
            )
            return ExecutionRecord(False, False, setup, sizing, label, {}, error=str(exc))

        if not self._cfg.is_live:
            log.info("[PAPER] Would submit create_order: %s", payload)
            await self._notify(setup, sizing, label, simulated=True)
            return ExecutionRecord(False, True, setup, sizing, label, payload)

        try:
            response = await self._gateway.create_order(request)
        except Exception as exc:  # noqa: BLE001
            log.error("create_order failed: %s", exc)
            await self._notifier.send(
                f"❌ <b>Order rejected</b>\n{esc(setup.direction.value)} "
                f"{sizing.lots} lots @ {setup.entry:.2f}\n<code>{esc(str(exc)[:400])}</code>",
                dedup_key=f"order-error-{label}",
            )
            return ExecutionRecord(False, False, setup, sizing, label, payload, error=str(exc))

        log.info("Order accepted: %s", response)
        await self._notify(setup, sizing, label, simulated=False)
        return ExecutionRecord(True, False, setup, sizing, label, payload, response=response)

    async def _notify(self, setup: TradeSetup, sizing: SizingResult,
                      label: str, *, simulated: bool) -> None:
        header = "📄 <b>PAPER ORDER</b>" if simulated else "🎯 <b>ORDER PLACED</b>"
        arrow = "🔻" if setup.direction.value == "SELL" else "🔺"
        narrative = "\n".join(f"• {esc(line)}" for line in setup.narrative)
        await self._notifier.send(
            f"{header}\n"
            f"{arrow} <b>{esc(setup.direction.value)} LIMIT {esc(self._gateway.active_name)}</b> "
            f"{sizing.lots} lots\n"
            f"<b>Entry</b> {setup.entry:.2f}\n"
            f"<b>SL</b> {setup.stop_loss:.2f}  "
            f"({setup.stop_distance / self._gateway.point_size:.0f}pt)\n"
            f"<b>TP</b> {setup.take_profit:.2f}  ({setup.risk_reward:.2f}R)\n"
            f"<b>Risk</b> {sizing.actual_risk:.2f} "
            f"({sizing.actual_risk / max(sizing.risk_amount, 1e-9) * self._cfg.risk_per_trade_pct:.2f}% "
            f"of balance)\n"
            f"<b>Window</b> {esc(setup.window)} | <code>{esc(label)}</code>\n\n"
            f"{narrative}"
        )
