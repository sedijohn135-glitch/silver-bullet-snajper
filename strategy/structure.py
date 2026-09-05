"""Step 2: displacement and the Market Structure Shift.

A sweep on its own is noise.  The setup only becomes tradeable when price
*displaces* away from the raided level with unusually large bodies and closes
through the opposing short-term structure - that is the algorithmic footprint
that something repriced, rather than another wick into the level.
"""

from __future__ import annotations

from typing import Optional, Sequence

from models import Candle
from strategy.liquidity import most_recent_swing
from strategy.models import Direction, MarketStructureShift, SwingPoint, Sweep
from utils.logging import get_logger

log = get_logger("strategy.structure")


def average_body(candles: Sequence[Candle], end_index: int, lookback: int = 20) -> float:
    """Mean candle body over the bars preceding ``end_index`` (the baseline)."""
    start = max(0, end_index - lookback)
    window = candles[start:end_index]
    if not window:
        return 0.0
    return sum(c.body for c in window) / len(window)


def find_reference_structure(
    candles: Sequence[Candle],
    swings: Sequence[SwingPoint],
    sweep: Sweep,
    *,
    fallback_lookback: int = 12,
) -> Optional[float]:
    """The level whose break confirms the shift.

    For a short (after a buy-side raid) that is the most recent swing low formed
    *before* the raid; if the fractal scan has nothing recent, fall back to the
    extreme of the last few bars so a clean, fast raid still qualifies.
    """
    want_high = sweep.direction is Direction.BUY
    swing = most_recent_swing(swings, is_high=want_high, before_index=sweep.index)
    if swing is not None and sweep.index - swing.index <= fallback_lookback * 2:
        return swing.price

    start = max(0, sweep.index - fallback_lookback)
    window = candles[start:sweep.index + 1]
    if not window:
        return None
    return max(c.high for c in window) if want_high else min(c.low for c in window)


def detect_mss(
    candles: Sequence[Candle],
    swings: Sequence[SwingPoint],
    sweep: Sweep,
    *,
    body_multiplier: float,
    min_displacement: float,
    max_bars_after_sweep: int = 20,
) -> Optional[MarketStructureShift]:
    """Confirm a displacement-driven structure break after the sweep."""
    level = find_reference_structure(candles, swings, sweep)
    if level is None:
        return None

    baseline = average_body(candles, sweep.index, lookback=20)
    if baseline <= 0:
        return None

    end = min(len(candles), sweep.index + max_bars_after_sweep + 1)
    for j in range(sweep.index + 1, end):
        candle = candles[j]
        broke = candle.close < level if sweep.direction is Direction.SELL else candle.close > level
        if not broke:
            continue

        # The break must be *driven*: at least one candle in the leg with a body
        # well above the recent average, in the direction of the trade.
        leg = candles[sweep.index:j + 1]
        aligned = [
            (idx, c) for idx, c in enumerate(leg, start=sweep.index)
            if (c.is_bearish if sweep.direction is Direction.SELL else c.is_bullish)
        ]
        if not aligned:
            continue
        strongest_index, strongest = max(aligned, key=lambda pair: pair[1].body)
        if strongest.body < baseline * body_multiplier:
            continue

        displacement = abs(sweep.extreme - candle.close)
        if displacement < min_displacement:
            continue

        return MarketStructureShift(
            index=j,
            ts=candle.ts,
            broken_level=level,
            leg_start=sweep.index,
            leg_end=j,
            displacement_index=strongest_index,
            displacement_points=displacement,
            direction=sweep.direction,
        )
    return None


def leg_extreme(candles: Sequence[Candle], mss: MarketStructureShift) -> float:
    """The wick extreme of the whole sweep+displacement structure.

    This - not the entry candle - is what the structural stop is measured from.
    """
    leg = candles[mss.leg_start:mss.leg_end + 1]
    if not leg:
        return 0.0
    if mss.direction is Direction.SELL:
        return max(c.high for c in leg)
    return min(c.low for c in leg)
