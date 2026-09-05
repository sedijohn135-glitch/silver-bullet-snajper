"""Pre-trade safety gates.

Each guard is a pure function over already-fetched account state: the engine
does the I/O once, the guards decide.  That keeps every rule unit-testable and
means a single analysis pass cannot issue a scatter of redundant API calls.

The gate is fail-*closed*: anything it cannot verify blocks the trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from mcp_client.token_info import TokenInfo
from models import PendingOrder, Position, Quote
from risk.daily import DailyPnL
from utils.logging import get_logger

log = get_logger("risk.guards")


@dataclass(frozen=True)
class GuardVerdict:
    allowed: bool
    code: str
    reason: str = ""

    @staticmethod
    def ok(code: str) -> "GuardVerdict":
        return GuardVerdict(True, code)

    @staticmethod
    def block(code: str, reason: str) -> "GuardVerdict":
        return GuardVerdict(False, code, reason)


def check_kill_switch(enabled: bool) -> GuardVerdict:
    if enabled:
        return GuardVerdict.block("kill_switch", "KILL_SWITCH is set; trading disabled")
    return GuardVerdict.ok("kill_switch")


def check_environment(
    trading_mode: str, token: TokenInfo, allow_live_environment: bool
) -> GuardVerdict:
    """Refuse real orders unless the operator has explicitly opted in.

    Two independent switches must both be thrown: ``TRADING_MODE=live`` and - if
    the Bearer token says the account is a *live* one - ``ALLOW_LIVE_ENVIRONMENT``.
    An undecodable token counts as live for safety purposes.
    """
    if trading_mode != "live":
        return GuardVerdict.block(
            "trading_mode",
            f"TRADING_MODE={trading_mode}; orders are simulated, not sent",
        )
    if token.is_demo:
        return GuardVerdict.ok("environment")
    if allow_live_environment:
        return GuardVerdict.ok("environment")
    descriptor = "LIVE" if token.is_live else "UNKNOWN"
    return GuardVerdict.block(
        "environment",
        f"Token environment is {descriptor}, not demo, and ALLOW_LIVE_ENVIRONMENT is not set",
    )


def check_balance(balance: float, minimum: float) -> GuardVerdict:
    if balance <= 0:
        return GuardVerdict.block("balance", f"Account balance is {balance:.2f}")
    if minimum > 0 and balance < minimum:
        return GuardVerdict.block(
            "balance", f"Balance {balance:.2f} is below the {minimum:.2f} floor"
        )
    return GuardVerdict.ok("balance")


def check_spread(quote: Quote, point_size: float, max_points: float) -> GuardVerdict:
    """Block entry when the spread is wide - it corrupts both risk and RR."""
    if point_size <= 0:
        return GuardVerdict.block("spread", "Point size is not configured")
    spread_points = quote.spread / point_size
    if spread_points < 0:
        return GuardVerdict.block(
            "spread", f"Inverted quote: bid {quote.bid:.2f} > ask {quote.ask:.2f}"
        )
    if spread_points > max_points:
        return GuardVerdict.block(
            "spread",
            f"Spread {spread_points:.1f}pt exceeds the {max_points:.0f}pt limit "
            f"(bid {quote.bid:.2f} / ask {quote.ask:.2f})",
        )
    return GuardVerdict.ok("spread")


def check_daily_drawdown(pnl: DailyPnL) -> GuardVerdict:
    if pnl.halted:
        return GuardVerdict.block("daily_drawdown", pnl.reason)
    return GuardVerdict.ok("daily_drawdown")


def check_window_exposure(
    positions: Sequence[Position],
    pending: Sequence[PendingOrder],
    *,
    window_label: str,
    symbol_id: Optional[int],
    symbol_name: str,
    block_any_symbol_exposure: bool,
) -> GuardVerdict:
    """One trade per Silver Bullet window - verified against the broker.

    The window label is written onto every order, so this survives a redeploy:
    in-memory state resets, the broker's order book does not.
    """
    for position in positions:
        if window_label and window_label in (position.label or ""):
            return GuardVerdict.block(
                "one_per_window",
                f"Position {position.position_id} already open for this window ({window_label})",
            )
    for order in pending:
        if window_label and window_label in (order.label or ""):
            return GuardVerdict.block(
                "one_per_window",
                f"Pending order {order.order_id} already placed for this window ({window_label})",
            )

    if not block_any_symbol_exposure:
        return GuardVerdict.ok("one_per_window")

    def _matches(sym_id: Optional[int], sym_name: str) -> bool:
        if symbol_id is not None and sym_id is not None:
            return sym_id == symbol_id
        return bool(sym_name) and sym_name.upper() == symbol_name.upper()

    for position in positions:
        if _matches(position.symbol_id, position.symbol_name):
            return GuardVerdict.block(
                "symbol_exposure",
                f"Already exposed to {symbol_name}: position {position.position_id} "
                f"({position.side} {position.volume})",
            )
    for order in pending:
        if _matches(order.symbol_id, order.symbol_name):
            return GuardVerdict.block(
                "symbol_exposure",
                f"Already exposed to {symbol_name}: pending order {order.order_id}",
            )
    return GuardVerdict.ok("one_per_window")


def first_blocker(verdicts: Sequence[GuardVerdict]) -> Optional[GuardVerdict]:
    return next((v for v in verdicts if not v.allowed), None)
