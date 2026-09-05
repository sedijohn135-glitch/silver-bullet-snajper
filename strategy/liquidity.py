"""Step 1 of the Silver Bullet: where liquidity rests, and when it gets raided.

Liquidity pools are the levels retail stops sit behind - session highs/lows,
previous-day extremes, equal highs/lows and unfilled higher-timeframe gaps.  The
setup begins when one of them is swept and immediately rejected.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Iterable, Optional, Sequence

from models import Candle
from strategy.models import Direction, LiquidityPool, PoolSide, Sweep, SwingPoint
from utils.logging import get_logger
from utils.sessions import LIQUIDITY_SESSIONS, session_ranges_before, to_local

log = get_logger("strategy.liquidity")


# ---------------------------------------------------------------------------
# Swings
# ---------------------------------------------------------------------------

def find_swings(candles: Sequence[Candle], strength: int = 2) -> list[SwingPoint]:
    """Fractal pivots: a high with ``strength`` lower highs on both sides."""
    swings: list[SwingPoint] = []
    if len(candles) < strength * 2 + 1:
        return swings
    for i in range(strength, len(candles) - strength):
        left = candles[i - strength:i]
        right = candles[i + 1:i + 1 + strength]
        pivot = candles[i]
        if all(pivot.high > c.high for c in left) and all(pivot.high >= c.high for c in right):
            swings.append(SwingPoint(i, pivot.ts, pivot.high, True))
        if all(pivot.low < c.low for c in left) and all(pivot.low <= c.low for c in right):
            swings.append(SwingPoint(i, pivot.ts, pivot.low, False))
    return swings


def most_recent_swing(swings: Sequence[SwingPoint], *, is_high: bool,
                      before_index: int) -> Optional[SwingPoint]:
    candidates = [s for s in swings if s.is_high is is_high and s.index <= before_index]
    return candidates[-1] if candidates else None


# ---------------------------------------------------------------------------
# Pools
# ---------------------------------------------------------------------------

def range_between(candles: Sequence[Candle], start: datetime,
                  end: datetime) -> Optional[tuple[float, float, datetime, datetime]]:
    """(high, low, high_ts, low_ts) of the candles inside [start, end)."""
    window = [c for c in candles if start <= c.ts < end]
    if not window:
        return None
    high_candle = max(window, key=lambda c: c.high)
    low_candle = min(window, key=lambda c: c.low)
    return high_candle.high, low_candle.low, high_candle.ts, low_candle.ts


def session_pools(candles: Sequence[Candle], now: datetime) -> list[LiquidityPool]:
    """Highs/lows of every liquidity session that has already begun today."""
    pools: list[LiquidityPool] = []
    for session, start, end in session_ranges_before(now, LIQUIDITY_SESSIONS):
        extremes = range_between(candles, start, end)
        if extremes is None:
            continue
        high, low, high_ts, low_ts = extremes
        pools.append(LiquidityPool(f"{session.name}_HIGH", high, PoolSide.BUYSIDE, high_ts))
        pools.append(LiquidityPool(f"{session.name}_LOW", low, PoolSide.SELLSIDE, low_ts))
    return pools


def previous_day_pools(candles: Sequence[Candle], now: datetime) -> list[LiquidityPool]:
    """PDH/PDL - the most reliably targeted levels on gold."""
    local_today = to_local(now).date()
    previous: list[Candle] = [c for c in candles if to_local(c.ts).date() < local_today]
    if not previous:
        return []
    last_day = to_local(previous[-1].ts).date()
    day_candles = [c for c in previous if to_local(c.ts).date() == last_day]
    if not day_candles:
        return []
    high_candle = max(day_candles, key=lambda c: c.high)
    low_candle = min(day_candles, key=lambda c: c.low)
    return [
        LiquidityPool("PDH", high_candle.high, PoolSide.BUYSIDE, high_candle.ts),
        LiquidityPool("PDL", low_candle.low, PoolSide.SELLSIDE, low_candle.ts),
    ]


def equal_level_pools(swings: Sequence[SwingPoint], tolerance: float,
                      *, min_touches: int = 2) -> list[LiquidityPool]:
    """Cluster swings that sit within ``tolerance`` of each other (EQH/EQL).

    Equal highs/lows are the cleanest liquidity on the chart: two or more stops
    stacked at the same price.
    """
    pools: list[LiquidityPool] = []
    for is_high in (True, False):
        group = sorted((s for s in swings if s.is_high is is_high), key=lambda s: s.price)
        cluster: list[SwingPoint] = []
        for swing in group:
            if cluster and abs(swing.price - cluster[-1].price) > tolerance:
                pools.extend(_pool_from_cluster(cluster, is_high, min_touches))
                cluster = []
            cluster.append(swing)
        pools.extend(_pool_from_cluster(cluster, is_high, min_touches))
    return pools


def _pool_from_cluster(cluster: list[SwingPoint], is_high: bool,
                       min_touches: int) -> list[LiquidityPool]:
    if len(cluster) < min_touches:
        return []
    # Use the extreme of the cluster: that is where the stops actually sit.
    price = max(s.price for s in cluster) if is_high else min(s.price for s in cluster)
    latest = max(cluster, key=lambda s: s.index)
    return [
        LiquidityPool(
            "EQH" if is_high else "EQL",
            price,
            PoolSide.BUYSIDE if is_high else PoolSide.SELLSIDE,
            latest.ts,
            strength=len(cluster),
        )
    ]


def swing_pools(swings: Sequence[SwingPoint], limit: int = 8) -> list[LiquidityPool]:
    """The most recent untouched swing highs/lows as generic liquidity."""
    highs = [s for s in swings if s.is_high][-limit:]
    lows = [s for s in swings if not s.is_high][-limit:]
    return [
        *(LiquidityPool("SWING_HIGH", s.price, PoolSide.BUYSIDE, s.ts) for s in highs),
        *(LiquidityPool("SWING_LOW", s.price, PoolSide.SELLSIDE, s.ts) for s in lows),
    ]


def filter_untapped(
    pools: Sequence[LiquidityPool],
    candles: Sequence[Candle],
    until: datetime,
    tolerance: float = 0.0,
) -> list[LiquidityPool]:
    """Drop levels whose liquidity has *already* been taken.

    Once price trades through a high, the stops behind it are gone - the level is
    spent, and a later wick past it is not a fresh raid.  Without this filter the
    Asian high stays "sweepable" all day even after London ran straight through
    it, and the bot ends up anchoring setups to levels that stopped mattering
    hours ago.
    """
    survivors: list[LiquidityPool] = []
    for pool in pools:
        if pool.ts is None:
            survivors.append(pool)   # unknown age: cannot judge, keep it
            continue
        after = [c for c in candles if pool.ts < c.ts < until]
        if pool.side is PoolSide.BUYSIDE:
            tapped = any(c.high > pool.price + tolerance for c in after)
        else:
            tapped = any(c.low < pool.price - tolerance for c in after)
        if not tapped:
            survivors.append(pool)
    return survivors


def build_pools(
    candles: Sequence[Candle],
    swings: Sequence[SwingPoint],
    now: datetime,
    *,
    equal_tolerance: float,
    extra_pools: Iterable[LiquidityPool] = (),
    tapped_until: Optional[datetime] = None,
    tap_tolerance: float = 0.0,
) -> list[LiquidityPool]:
    """Assemble every liquidity level worth watching, de-duplicated by price.

    ``tapped_until`` enables the spent-liquidity filter: levels already traded
    through before that moment are discarded (see :func:`filter_untapped`).
    """
    pools = [
        *session_pools(candles, now),
        *previous_day_pools(candles, now),
        *equal_level_pools(swings, equal_tolerance),
        *swing_pools(swings),
        *extra_pools,
    ]
    if tapped_until is not None:
        pools = filter_untapped(pools, candles, tapped_until, tap_tolerance)
    return dedupe_pools(pools, equal_tolerance / 2)


#: Lower number == more informative label, kept in preference when two pools
#: describe the same price.  "ASIA_HIGH" tells the operator far more than a
#: generic "SWING_HIGH" at the same level.
_POOL_PRIORITY: dict[str, int] = {
    "PDH": 0, "PDL": 0,
    "ASIA_HIGH": 1, "ASIA_LOW": 1, "LONDON_HIGH": 1, "LONDON_LOW": 1,
    "NEWYORK_AM_HIGH": 1, "NEWYORK_AM_LOW": 1,
    "EQH": 2, "EQL": 2,
    "SWING_HIGH": 3, "SWING_LOW": 3,
    "HTF_FVG": 4,
}


def dedupe_pools(pools: Sequence[LiquidityPool], tolerance: float) -> list[LiquidityPool]:
    """Collapse levels that are effectively the same price.

    The surviving pool keeps the most descriptive name but inherits the highest
    strength of the group, so a session high that also happens to be an equal
    high is still recognised as the stronger level.
    """
    ordered = sorted(pools, key=lambda p: (_POOL_PRIORITY.get(p.name, 9), -p.strength, p.name))
    kept: list[LiquidityPool] = []
    for pool in ordered:
        match = next(
            (i for i, other in enumerate(kept)
             if other.side is pool.side and abs(other.price - pool.price) <= tolerance),
            None,
        )
        if match is None:
            kept.append(pool)
            continue
        existing = kept[match]
        if pool.strength > existing.strength:
            kept[match] = replace(existing, strength=pool.strength)
    return kept


# ---------------------------------------------------------------------------
# Sweep detection
# ---------------------------------------------------------------------------

def detect_sweeps(
    candles: Sequence[Candle],
    pools: Sequence[LiquidityPool],
    *,
    since: datetime,
    min_penetration: float,
    close_back_bars: int = 3,
    limit: int = 8,
) -> list[Sweep]:
    """All valid liquidity raids since ``since``, best candidate first.

    A sweep is only valid if price closes back on the *origin* side of the pool
    within ``close_back_bars`` - otherwise it is a genuine breakout, not a raid,
    and fading it is how accounts die.

    Candidates are ranked by how meaningful the raided level is (previous-day and
    session extremes before generic swings) and then by recency.  Ranking matters:
    a displacement leg will often trip a minor swing high *on its way down*, and
    naively taking the most recent sweep would anchor the whole setup - stop
    included - to the middle of the impulse instead of its origin.
    """
    if not candles or not pools:
        return []

    start_index = next((i for i, c in enumerate(candles) if c.ts >= since), None)
    if start_index is None:
        return []

    found: dict[tuple[str, float], Sweep] = {}
    for i in range(start_index, len(candles)):
        candle = candles[i]
        for pool in pools:
            if not pool.sweepable:
                continue
            if pool.side is PoolSide.BUYSIDE:
                if candle.high < pool.price + min_penetration:
                    continue
                closed_back = _first_close_back(candles, i, close_back_bars,
                                               below=True, level=pool.price)
                if closed_back is None:
                    continue
                sweep = Sweep(pool, i, candle.ts, candle.high, closed_back, Direction.SELL)
            else:
                if candle.low > pool.price - min_penetration:
                    continue
                closed_back = _first_close_back(candles, i, close_back_bars,
                                               below=False, level=pool.price)
                if closed_back is None:
                    continue
                sweep = Sweep(pool, i, candle.ts, candle.low, closed_back, Direction.BUY)

            # Keep the deepest raid per pool: that wick is what the stop hides behind.
            key = (pool.name, round(pool.price, 5))
            previous = found.get(key)
            if previous is None or sweep.penetration > previous.penetration:
                found[key] = sweep

    # Ranking: significance of the level, then recency, then the *tightest*
    # raid.  The last term matters when one wick takes several levels at once -
    # the highest buy-side pool taken is the meaningful one, because reaching it
    # required running through everything below, and it is the level closest to
    # the wick tip that the structural stop will sit behind.
    ranked = sorted(
        found.values(),
        key=lambda s: (_POOL_PRIORITY.get(s.pool.name, 9), -s.index, s.penetration),
    )
    return ranked[:limit]


def detect_sweep(
    candles: Sequence[Candle],
    pools: Sequence[LiquidityPool],
    *,
    since: datetime,
    min_penetration: float,
    close_back_bars: int = 3,
) -> Optional[Sweep]:
    """The single best sweep candidate (see :func:`detect_sweeps`)."""
    sweeps = detect_sweeps(candles, pools, since=since, min_penetration=min_penetration,
                           close_back_bars=close_back_bars, limit=1)
    return sweeps[0] if sweeps else None


def _first_close_back(candles: Sequence[Candle], index: int, horizon: int,
                      *, below: bool, level: float) -> Optional[int]:
    for j in range(index, min(index + horizon + 1, len(candles))):
        close = candles[j].close
        if (below and close < level) or (not below and close > level):
            return j
    return None


def draw_on_liquidity(
    pools: Sequence[LiquidityPool],
    direction: Direction,
    entry: float,
    stop_loss: float,
    *,
    min_rr: float,
    min_distance: float = 0.0,
) -> Optional[tuple[LiquidityPool, float]]:
    """Pick the nearest liquidity pool that still pays at least ``min_rr``.

    ICT targets the *next* pool of opposing liquidity, so we walk outwards from
    entry and take the first one that clears the risk/reward floor rather than
    reaching for the furthest target.
    """
    risk = abs(entry - stop_loss)
    if risk <= 0:
        return None

    if direction is Direction.SELL:
        candidates = [p for p in pools if p.side is PoolSide.SELLSIDE and p.price < entry - min_distance]
        candidates.sort(key=lambda p: entry - p.price)   # nearest first
    else:
        candidates = [p for p in pools if p.side is PoolSide.BUYSIDE and p.price > entry + min_distance]
        candidates.sort(key=lambda p: p.price - entry)

    for pool in candidates:
        reward = abs(pool.price - entry)
        rr = reward / risk
        if rr >= min_rr:
            return pool, rr
    return None
