"""Step 3: the Fair Value Gap left by the displacement leg.

A 3-candle imbalance where candle 1 and candle 3 do not overlap: price moved so
quickly that a band of prices never traded on both sides.  The Silver Bullet
entry is a limit order back at that band.
"""

from __future__ import annotations

from typing import Optional, Sequence

from models import Candle
from strategy.models import Direction, FairValueGap
from utils.logging import get_logger

log = get_logger("strategy.fvg")


def find_fvgs(
    candles: Sequence[Candle],
    direction: Direction,
    *,
    start_index: int = 0,
    end_index: Optional[int] = None,
    min_size: float = 0.0,
) -> list[FairValueGap]:
    """All qualifying FVGs whose middle candle lies in [start_index, end_index]."""
    gaps: list[FairValueGap] = []
    last = (len(candles) - 2) if end_index is None else min(end_index, len(candles) - 2)
    for middle in range(max(1, start_index), last + 1):
        first, third = candles[middle - 1], candles[middle + 1]
        if direction is Direction.SELL:
            # Bearish gap: candle 1's low sits above candle 3's high.
            top, bottom = first.low, third.high
        else:
            # Bullish gap: candle 1's high sits below candle 3's low.
            top, bottom = third.low, first.high
        if top - bottom < max(min_size, 0.0) or top <= bottom:
            continue
        gaps.append(
            FairValueGap(
                direction=direction,
                top=top,
                bottom=bottom,
                first_index=middle - 1,
                middle_index=middle,
                third_index=middle + 1,
                ts=candles[middle].ts,
            )
        )
    return gaps


def mitigation_ratio(candles: Sequence[Candle], fvg: FairValueGap) -> float:
    """How much of the gap has already been traded back into (0.0 - 1.0+)."""
    after = candles[fvg.third_index + 1:]
    if not after or fvg.size <= 0:
        return 0.0
    if fvg.direction is Direction.SELL:
        deepest = max(c.high for c in after)
        return max(0.0, (deepest - fvg.bottom) / fvg.size)
    deepest = min(c.low for c in after)
    return max(0.0, (fvg.top - deepest) / fvg.size)


def select_entry_fvg(
    candles: Sequence[Candle],
    gaps: Sequence[FairValueGap],
    *,
    entry_mode: str,
    current_price: float,
    max_mitigation: float = 0.5,
) -> Optional[FairValueGap]:
    """Choose the FVG to trade.

    Rules, in order:
    * discard gaps already more than ``max_mitigation`` filled - the imbalance
      they represented has largely been rebalanced already;
    * discard gaps whose entry edge price has already been passed, since a limit
      order there would never be a retracement entry;
    * of what remains, take the one nearest current price (first to be tested).
    """
    viable: list[tuple[float, FairValueGap]] = []
    for gap in gaps:
        if mitigation_ratio(candles, gap) > max_mitigation:
            continue
        entry = gap.entry_for(entry_mode)
        if gap.direction is Direction.SELL and entry <= current_price:
            continue   # price is already above the sell entry
        if gap.direction is Direction.BUY and entry >= current_price:
            continue   # price is already below the buy entry
        viable.append((abs(entry - current_price), gap))

    if not viable:
        return None
    viable.sort(key=lambda pair: pair[0])
    return viable[0][1]


def htf_unfilled_gaps(
    candles: Sequence[Candle], *, min_size: float = 0.0, limit: int = 6
) -> list[FairValueGap]:
    """Unfilled higher-timeframe gaps in both directions.

    These are magnets: an unbalanced HTF gap is a legitimate draw on liquidity,
    so they feed the take-profit target list alongside session highs and lows.
    """
    out: list[FairValueGap] = []
    for direction in (Direction.SELL, Direction.BUY):
        for gap in find_fvgs(candles, direction, min_size=min_size):
            if mitigation_ratio(candles, gap) < 0.5:
                out.append(gap)
    out.sort(key=lambda g: g.ts, reverse=True)
    return out[:limit]
