"""The 24/7 control loop.

Shape of a tick:

* outside a Silver Bullet window -> housekeeping only (monitor open trades,
  cancel stale limits, heartbeat the connection);
* inside a window -> fetch state once, run every guard, analyse, size, execute.

Guards run *before* analysis (so a halted day costs one cheap check instead of a
full candle download) and the exposure/spread checks run *again* immediately
before submitting, because a minute can pass between the two.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from config import Config
from engine.executor import OrderExecutor
from engine.monitor import TradeMonitor
from engine.state import BotState, StateStore
from mcp_client.ctrader import CTraderGateway
from mcp_client.token_info import TokenInfo
from mcp_client.transport import ConnectionState, MCPConnection
from risk.daily import compute_daily_pnl
from risk.guards import (
    GuardVerdict, check_balance, check_daily_drawdown, check_environment,
    check_kill_switch, check_spread, check_window_exposure, first_blocker,
)
from risk.sizing import SizingResult, calculate_position_size
from strategy.models import Direction, TradeSetup
from strategy.silver_bullet import MarketSnapshot, SilverBulletStrategy
from utils.logging import get_logger
from utils.prices import round_price
from symbols import symbol_for_day
from utils.sessions import (
    active_window, local_day_start_utc, next_window_start, now_utc, to_local, window_key,
)
from utils.telegram import TelegramNotifier, esc

log = get_logger("engine")


class SilverBulletBot:
    def __init__(self, cfg: Config, token: TokenInfo,
                 notifier: TelegramNotifier) -> None:
        self._cfg = cfg
        self._token = token
        self._notifier = notifier

        self.connection = MCPConnection(cfg, notifier)
        self.gateway = CTraderGateway(cfg, self.connection)
        self.state = BotState()
        self.store = StateStore(cfg.state_dir)
        self.strategy = SilverBulletStrategy(cfg)
        self._active_symbol: Optional[str] = None
        self.monitor = TradeMonitor(cfg, self.gateway, notifier, self.state)
        self.executor = OrderExecutor(cfg, self.gateway, notifier)

        self._bootstrapped_for: Optional[int] = None
        self._stopping = asyncio.Event()
        self._logged_block: dict[str, str] = {}

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self.store.load(self.state)
        await self.connection.start()

    async def stop(self) -> None:
        self._stopping.set()
        self.store.save(self.state)
        await self.connection.stop()

    async def run(self) -> None:
        """The forever loop.  Nothing in here is allowed to raise."""
        await self.start()
        log.info(
            "Silver Bullet bot running | symbol=%s mode=%s %s | "
            "windows 09:00-10:00, 16:00-17:00, 20:00-21:00 Europe/Tirane",
            self._cfg.symbol, self._cfg.trading_mode.upper(), self._token.describe(),
        )
        while not self._stopping.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop outlives every error
                self.state.last_error = str(exc)[:400]
                log.exception("Unhandled error in tick: %s", exc)
            try:
                await asyncio.wait_for(self._stopping.wait(),
                                       timeout=self._cfg.poll_interval_seconds)
            except asyncio.TimeoutError:
                pass

    # -- ticks -------------------------------------------------------------

    async def _tick(self) -> None:
        now = now_utc()
        self.state.last_tick = now

        if not self.connection.is_ready:
            # The supervisor is already reconnecting; do not pile on.
            log.debug("Connection not ready (%s); skipping tick", self.connection.state.value)
            return

        # Re-bootstrap after every reconnect: symbol ids and scaling must be
        # re-derived against the session we are actually talking to now.
        if self._bootstrapped_for != self.connection.stats.connects:
            await self.gateway.bootstrap()
            self._bootstrapped_for = self.connection.stats.connects
            self._active_symbol = None
            await self._announce_startup()
            await self._preflight()

        # XAUUSD is shut from Friday evening to Sunday evening while BTCUSD runs
        # continuously, so the instrument is chosen per local day.
        if not self._select_instrument(now):
            return

        window = active_window(now)
        day_start = local_day_start_utc(now)

        report = await self.monitor.poll(day_start)
        active_label = self._label(window, now) if window else None
        if self._cfg.cancel_unfilled_at_window_end:
            await self.monitor.cancel_stale_orders(report.pending, window_label=active_label)

        if window is None:
            self.state.halted_reason = ""
            log.debug("Outside Silver Bullet windows; next opens %s",
                      to_local(next_window_start(now)).strftime("%Y-%m-%d %H:%M %Z"))
            return

        await self._analyse_window(window, now, report, day_start)

    async def _analyse_window(self, window, now: datetime, report, day_start: datetime) -> None:
        cfg = self._cfg
        label = self._label(window, now)

        if label in self.state.executed_windows:
            log.debug("Window %s already executed at %s",
                      label, self.state.executed_windows[label])
            return

        balance = await self.gateway.get_balance()
        quote = await self.gateway.get_quote()
        pnl = compute_daily_pnl(
            balance=balance.balance,
            deals=report.deals,
            positions=report.positions,
            day_start=day_start,
            max_drawdown_pct=cfg.daily_max_drawdown_pct,
            include_open=cfg.include_open_pnl_in_drawdown,
        )

        verdicts = [
            check_kill_switch(cfg.kill_switch),
            check_balance(balance.balance, cfg.min_account_balance),
            check_daily_drawdown(pnl),
            check_window_exposure(
                report.positions, report.pending,
                window_label=label,
                symbol_id=self.gateway.symbol.symbol_id if self.gateway.symbol else None,
                symbol_name=self.gateway.active_name,
                block_any_symbol_exposure=cfg.block_if_any_symbol_exposure,
            ),
            check_spread(quote, self.gateway.point_size,
                         self.gateway.profile.max_spread_points),
        ]
        blocker = first_blocker(verdicts)
        if blocker is not None:
            await self._report_block(label, blocker, pnl)
            return
        self.state.halted_reason = ""

        # --- analysis ---------------------------------------------------
        window_start, _ = window.bounds_on(to_local(now).date())
        snapshot = MarketSnapshot(
            entry_candles=await self.gateway.get_candles(cfg.entry_timeframe, cfg.entry_bars),
            structure_candles=await self.gateway.get_candles(cfg.structure_timeframe, cfg.structure_bars),
            htf_candles=await self.gateway.get_candles(cfg.htf_timeframe, cfg.htf_bars),
            quote=quote,
            now=now,
            window_name=window.name,
            window_start=window_start.astimezone(timezone.utc),
        )
        result = self.strategy.analyse(snapshot)
        self.state.last_analysis = now
        self.state.last_analysis_window = label
        self.state.last_rejections = list(result.rejections)

        if not result.found:
            # The loop polls every few seconds; only speak when the picture
            # actually changes, otherwise a quiet window buries the log.
            summary = " | ".join(result.rejections) or "no candidates"
            if self._logged_block.get(f"{label}:analysis") != summary:
                self._logged_block[f"{label}:analysis"] = summary
                log.info("[%s] No setup: %s", label, summary)
            else:
                log.debug("[%s] No setup (unchanged)", label)
            return

        setup = result.setup
        log.info("[%s] SETUP: %s", label, " | ".join(setup.narrative))

        # --- sizing -----------------------------------------------------
        step, minimum, maximum = self._volume_limits()
        sizing = calculate_position_size(
            balance=balance.balance,
            risk_pct=cfg.risk_per_trade_pct,
            stop_distance=setup.stop_distance,
            contract_size=self.gateway.profile.contract_size,
            volume_step=step,
            min_volume=minimum,
            max_volume=maximum,
            entry_price=setup.entry,
            max_notional_leverage=cfg.max_notional_leverage,
            fixed_lots=cfg.fixed_lot_size,
            max_risk_pct=cfg.max_risk_pct,
            layers=cfg.entry_layers,
        )
        if not sizing.accepted:
            log.warning("[%s] Setup found but not sizeable: %s", label, sizing.reason)
            await self._notifier.send(
                f"⚠️ <b>Setup skipped</b> ({esc(label)})\n{esc(sizing.reason)}",
                dedup_key=f"sizing-{label}",
            )
            return

        # --- final pre-flight -------------------------------------------
        fresh_quote = await self.gateway.get_quote()
        spread_now = check_spread(fresh_quote, self.gateway.point_size,
                                  self.gateway.profile.max_spread_points)
        if not spread_now.allowed:
            log.warning("[%s] Spread widened before submission: %s", label, spread_now.reason)
            return
        positions = await self.gateway.get_positions()
        pending = await self.gateway.get_pending_orders()
        exposure_now = check_window_exposure(
            positions, pending, window_label=label,
            symbol_id=self.gateway.symbol.symbol_id if self.gateway.symbol else None,
            symbol_name=self.gateway.active_name,
            block_any_symbol_exposure=cfg.block_if_any_symbol_exposure,
        )
        if not exposure_now.allowed:
            log.warning("[%s] Exposure appeared before submission: %s", label, exposure_now.reason)
            return

        record = await self.executor.execute(setup, sizing, label)
        if record.submitted or record.simulated:
            # Claim the window even in paper mode so a dry run reproduces the
            # real one-trade-per-window cadence.
            self.state.executed_windows[label] = now.isoformat()
            self.state.trades_today += 1
            self.store.save(self.state)

    # -- helpers -----------------------------------------------------------

    async def _preflight(self) -> None:
        """Build - and deliberately never send - one order per instrument.

        The order payload is assembled from the live schema, so a mistake in it
        only surfaces at the exact moment a setup appears, which is the worst
        possible time to discover that (say) volume rounds to zero. This runs the
        whole construction path at boot against the smallest permitted size and
        prints the result, turning an unknown into a log line the operator can
        check immediately.

        It proves the payload is well-formed and shows the real numbers. It does
        not prove the broker will accept it - only a live order does that.
        """
        for name in list(self.gateway.symbols):
            try:
                self.gateway.set_active(name)
                profile = self.gateway.profile
                quote = await self.gateway.get_quote()
                step, minimum, _ = self._volume_limits()

                # A sell limit parked far above the market: representative in
                # shape, and impossible to fill even if it were sent.
                stop_gap = profile.points_to_price(profile.sl_buffer_points)
                entry = round_price(quote.ask + stop_gap * 5, self.gateway.point_size)
                stop_loss = round_price(entry + stop_gap, self.gateway.point_size)
                take_profit = round_price(entry - stop_gap * 2, self.gateway.point_size)

                request = self.executor.build_request(
                    TradeSetup(
                        direction=Direction.SELL, entry=entry, stop_loss=stop_loss,
                        take_profit=take_profit, risk_reward=2.0, sweep=None, mss=None,
                        fvg=None, target=None, window="PREFLIGHT",
                    ),
                    SizingResult(minimum, 0.0, 0.0, 0.0, True),
                    f"{self._cfg.order_label_prefix}-PREFLIGHT",
                )
                payload = self.gateway.describe_order_payload(request)
                log.info(
                    "PREFLIGHT %s (NOT sent) | min %s lots -> wire volume %r | %s",
                    name, minimum, payload.get(self._order_volume_field(), "?"), payload,
                )
                if payload.get(self._order_volume_field()) in (0, None):
                    log.error(
                        "PREFLIGHT %s: volume resolves to %r - no order could ever be "
                        "placed. Set VOLUME_UNIT_MODE explicitly.",
                        name, payload.get(self._order_volume_field()),
                    )
                    await self._notifier.send(
                        f"🚨 <b>Preflight failed</b> ({esc(name)})\n"
                        f"create_order volume resolves to "
                        f"<code>{esc(payload.get(self._order_volume_field()))}</code> - "
                        f"no order can be placed. Set VOLUME_UNIT_MODE.",
                        dedup_key=f"preflight-volume-{name}",
                    )
            except Exception as exc:  # noqa: BLE001 - a preflight must never stop the bot
                log.error("PREFLIGHT %s could not build an order payload: %s", name, exc)
                await self._notifier.send(
                    f"⚠️ <b>Preflight failed</b> ({esc(name)})\n<code>{esc(str(exc)[:300])}</code>",
                    dedup_key=f"preflight-{name}",
                )
        # Leave the active instrument to the normal daily selection.
        self._active_symbol = None

    def _order_volume_field(self) -> str:
        return self.gateway._property_for("volume") or "volume"

    def _select_instrument(self, now: datetime) -> bool:
        """Point the gateway and strategy at today's instrument.

        Returns False when there is nothing to trade today (weekend trading
        disabled), which parks the bot for the day without touching the market.
        """
        local_day = to_local(now).date()
        wanted = symbol_for_day(
            local_day,
            weekday_symbol=self._cfg.symbol,
            weekend_symbol=self._cfg.weekend_symbol,
            weekend_enabled=self._cfg.weekend_trading,
        )
        if wanted is None:
            if self._active_symbol is not None:
                log.info("Weekend trading disabled; idling until Monday.")
                self._active_symbol = None
            return False

        if wanted == self._active_symbol:
            return True

        try:
            self.gateway.set_active(wanted)
        except Exception as exc:  # noqa: BLE001 - a missing symbol must not crash the loop
            log.error("Cannot activate %s: %s", wanted, exc)
            self.state.last_error = f"activate {wanted}: {exc}"
            return False

        self.strategy.set_instrument(self.gateway.profile, self.gateway.point_size)
        self._active_symbol = wanted
        log.info("Trading %s today (%s)", wanted, local_day.strftime("%A"))
        return True

    def _volume_limits(self) -> tuple[float, float, float]:
        """Broker volume constraints, falling back to configuration.

        The symbol listing does not always carry these, so config supplies the
        floor - but when the broker does publish them they win, since an order
        off the volume grid is rejected outright.
        """
        profile = self.gateway.profile
        symbol = self.gateway.symbol
        step = (symbol.volume_step if symbol and symbol.volume_step else profile.volume_step)
        minimum = (symbol.min_volume if symbol and symbol.min_volume else profile.min_volume)
        maximum = (symbol.max_volume if symbol and symbol.max_volume else profile.max_volume)
        return step, minimum, maximum

    def _label(self, window, now: datetime) -> str:
        """Order label: prefix, instrument, local day, window.

        The instrument is part of the key so the one-trade-per-window rule is
        scoped per symbol, and so a gold order can never be mistaken for a
        Bitcoin one when the labels are read back off the broker.
        """
        return (f"{self._cfg.order_label_prefix}-{self.gateway.active_name}"
                f"-{window_key(window, now)}")

    async def _report_block(self, label: str, blocker: GuardVerdict, pnl) -> None:
        """Log once per (window, guard) so a blocked window does not spam."""
        self.state.halted_reason = blocker.reason
        key = f"{label}:{blocker.code}"
        if self._logged_block.get(key) == blocker.reason:
            return
        self._logged_block[key] = blocker.reason
        log.warning("[%s] Blocked by %s: %s", label, blocker.code, blocker.reason)
        if blocker.code in ("daily_drawdown", "kill_switch", "balance"):
            await self._notifier.send(
                f"🛑 <b>Trading halted</b> ({esc(blocker.code)})\n{esc(blocker.reason)}",
                dedup_key=key,
            )

    async def _announce_startup(self) -> None:
        cfg = self._cfg
        env_verdict = check_environment(cfg.trading_mode, self._token, cfg.allow_live_environment)
        live = env_verdict.allowed and cfg.is_live
        mode_line = "LIVE ORDERS ENABLED" if live else f"SIMULATED ({esc(env_verdict.reason)})"
        icon = "🔴" if live else "🟡"

        instruments = f"{esc(cfg.symbol)} Mon-Fri"
        if cfg.weekend_trading and cfg.weekend_symbol:
            instruments += f", {esc(cfg.weekend_symbol)} Sat/Sun"
        else:
            instruments += " (weekend trading off)"
        specs = "\n".join(
            f"  <code>{esc(p.describe())}</code>" for p in (cfg.profiles or {}).values()
        )

        tools = self.connection.catalog.names
        await self._notifier.send(
            f"{icon} <b>ICT Silver Bullet bot online</b>\n"
            f"<b>Instruments</b> {instruments}\n"
            f"{specs}\n"
            f"<b>Account</b> {esc(self._token.describe())}\n"
            f"<b>Mode</b> {mode_line}\n"
            f"<b>Risk</b> {cfg.risk_per_trade_pct:.2f}%/trade, "
            + (f"{cfg.daily_max_drawdown_pct:.2f}% daily stop"
               if cfg.daily_max_drawdown_pct > 0 else "<b>daily stop OFF</b>")
            + f", min {cfg.min_rr:.1f}R\n"
            f"<b>Windows</b> 09:00-10:00, 16:00-17:00, 20:00-21:00 Europe/Tirane\n"
            f"<b>MCP</b> {len(tools)} tools, session "
            f"<code>{esc(self.connection.stats.session_id or 'n/a')}</code>",
            dedup_key=f"startup-{self.connection.stats.connects}",
        )

    # -- status ------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        window = active_window()
        return {
            "healthy": self.connection.state in (ConnectionState.READY, ConnectionState.CONNECTING),
            "mode": self._cfg.trading_mode,
            "environment": self._token.environment or "unknown",
            "symbol": self.gateway.active_name,
            "weekday_symbol": self._cfg.symbol,
            "weekend_symbol": self._cfg.weekend_symbol if self._cfg.weekend_trading else None,
            "active_window": window.name if window else None,
            "next_window": next_window_start().isoformat(),
            "connection": self.connection.snapshot(),
            "state": self.state.snapshot(),
        }
