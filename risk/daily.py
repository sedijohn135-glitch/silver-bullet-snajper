"""Daily drawdown accounting.

Realised P&L is recomputed from the broker's own deal history on every pass
rather than tallied in memory: a Railway redeploy wipes local state, and a bot
that "forgot" it was already 2.8% down would happily open another trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from models import Deal, Position
from utils.logging import get_logger

log = get_logger("risk.daily")


@dataclass(frozen=True)
class DailyPnL:
    realized: float
    unrealized: float
    day_start_balance: float
    current_balance: float
    loss_pct: float          # positive number = down on the day
    halted: bool
    reason: str = ""

    @property
    def total(self) -> float:
        return self.realized + self.unrealized


def compute_daily_pnl(
    *,
    balance: float,
    deals: Sequence[Deal],
    positions: Sequence[Position],
    day_start: datetime,
    max_drawdown_pct: float,
    include_open: bool = True,
) -> DailyPnL:
    """Assess the day against the drawdown limit.

    ``balance`` is the broker's realised balance, so today's realised P&L is
    already baked into it; the day's opening balance is recovered by backing it
    out.  Floating P&L on open positions is included by default - a 3% loss that
    is merely unrealised will still be a 3% loss when the stop is hit.
    """
    realized = sum(d.net_profit for d in deals if d.executed_at and d.executed_at >= day_start)
    unrealized = sum(p.profit or 0.0 for p in positions) if include_open else 0.0

    day_start_balance = balance - realized
    if day_start_balance <= 0:
        return DailyPnL(
            realized, unrealized, day_start_balance, balance, 100.0, True,
            f"Opening balance for the day computes to {day_start_balance:.2f}; refusing to trade",
        )

    change = realized + unrealized
    loss_pct = max(0.0, -change / day_start_balance * 100.0)

    # A limit of 0 means "no daily halt". It has to be checked explicitly:
    # `loss_pct >= 0` is true for every possible day, so a naive comparison
    # would turn the switch that disables the guard into one that halts always.
    halted = max_drawdown_pct > 0 and loss_pct >= max_drawdown_pct
    reason = ""
    if halted:
        reason = (
            f"Daily drawdown {loss_pct:.2f}% has reached the {max_drawdown_pct:.2f}% limit "
            f"(realised {realized:+.2f}, open {unrealized:+.2f} on a "
            f"{day_start_balance:.2f} opening balance)"
        )
    return DailyPnL(realized, unrealized, day_start_balance, balance, loss_pct, halted, reason)
