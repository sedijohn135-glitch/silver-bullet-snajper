"""The ICT Silver Bullet strategy, end to end.

Pipeline (all five steps must pass, in order, or no trade is taken):

1. **Liquidity sweep** - a session/previous-day/equal high or low is raided and
   rejected, just before or inside the execution window.
2. **Displacement + MSS** - price drives away from the raid with outsized bodies
   and closes through the opposing short-term structure.
3. **Fair Value Gap** - the 3-candle imbalance inside that displacement leg is
   the entry zone.
4. **Entry** - a limit order at the FVG edge (never a market chase).
5. **Stops and targets** - a structural stop 20 points beyond the sweep wick, and
   a target at the next draw on liquidity that pays at least 1:2.

Every rejection is recorded with a reason so the log explains *why* a window
passed without a trade - silence is the enemy of trust in an automated system.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence

from config import Config
from models import Candle, Quote
from strategy.fvg import find_fvgs, htf_unfilled_gaps, select_entry_fvg
from strategy.liquidity import (
    build_pools, detect_sweeps, draw_on_liquidity, find_swings, previous_day_pools,
)
from strategy.models import (
    AnalysisResult, Direction, FairValueGap, LiquidityPool, PoolSide, Sweep,
    SwingPoint, TradeSetup,
)
from strategy.structure import detect_mss, leg_extreme
from utils.logging import get_logger
from utils.prices import round_price

log = get_logger("strategy.silverbullet")


@dataclass
class MarketSnapshot:
    """Everything one analysis pass needs, already normalised to real prices."""

    entry_candles: Sequence[Candle]      # M1 by default - execution timeframe
    structure_candles: Sequence[Candle]  # M5 - session ranges, FVG fallback
    htf_candles: Sequence[Candle]        # H1 - previous day levels, HTF gaps
    quote: Quote
    now: datetime
    window_name: str
    window_start: datetime


class SilverBulletStrategy:
    def __init__(self, cfg: Config, point_size: Optional[float] = None) -> None:
        self._cfg = cfg
        self.point_size = point_size or cfg.point_size

    # -- helpers -----------------------------------------------------------

    def points(self, count: float) -> float:
        return count * self.point_size

    def to_points(self, price_delta: float) -> float:
        return price_delta / self.point_size if self.point_size else 0.0

    # -- main entry point --------------------------------------------------

    def analyse(self, snapshot: MarketSnapshot) -> AnalysisResult:
        result = AnalysisResult(candles_used=len(snapshot.entry_candles))
        candles = list(snapshot.entry_candles)
        if len(candles) < 30:
            return result.reject(f"Not enough candles to analyse ({len(candles)})")

        cfg = self._cfg
        swings = find_swings(candles, cfg.swing_strength)

        # --- Step 1: liquidity ------------------------------------------
        lookback_start = snapshot.window_start - timedelta(minutes=cfg.sweep_lookback_minutes)
        # Previous-day levels come from the HTF series, which reaches back far
        # enough to actually contain yesterday.
        extra_pools = [
            *self._htf_gap_pools(snapshot.htf_candles),
            *previous_day_pools(snapshot.htf_candles, snapshot.now),
        ]
        structure = snapshot.structure_candles or candles
        pools = build_pools(
            structure,
            swings,
            snapshot.now,
            equal_tolerance=self.points(cfg.equal_level_tolerance_points),
            extra_pools=extra_pools,
            tapped_until=lookback_start,
            tap_tolerance=self.points(cfg.min_sweep_penetration_points),
        )
        result.pools_considered = len(pools)
        if not pools:
            return result.reject("No liquidity pools could be built from the data")

        sweeps = detect_sweeps(
            candles,
            pools,
            since=lookback_start,
            min_penetration=self.points(cfg.min_sweep_penetration_points),
        )
        if not sweeps:
            return result.reject(
                f"No liquidity sweep since {lookback_start:%H:%M} UTC "
                f"({len(pools)} pools watched)"
            )

        # Evaluate candidates in significance order and take the first that
        # completes the whole chain; a sweep that never produced displacement is
        # not a Silver Bullet, no matter how clean the raid looked.
        for sweep in sweeps:
            completed = self._evaluate(sweep, candles, swings, pools, snapshot, result)
            if completed is not None:
                result.setup = completed
                return result
        return result

    def _evaluate(
        self,
        sweep: Sweep,
        candles: Sequence[Candle],
        swings: Sequence[SwingPoint],
        pools: Sequence[LiquidityPool],
        snapshot: MarketSnapshot,
        result: AnalysisResult,
    ) -> Optional[TradeSetup]:
        """Try to build a complete setup from one sweep candidate (steps 2-5)."""
        cfg = self._cfg

        # --- Step 2: displacement + MSS ---------------------------------
        mss = detect_mss(
            candles,
            swings,
            sweep,
            body_multiplier=cfg.displacement_body_mult,
            min_displacement=self.points(cfg.min_displacement_points),
        )
        if mss is None:
            result.reject(f"Swept {sweep.pool} at {sweep.ts:%H:%M} but no displacement/MSS confirmed")
            return None

        # --- Step 3: fair value gap -------------------------------------
        price_now = snapshot.quote.ask if sweep.direction is Direction.SELL else snapshot.quote.bid
        fvg = self._select_fvg(candles, snapshot, mss, price_now)
        if fvg is None:
            result.reject(f"MSS confirmed at {mss.ts:%H:%M} but no valid unmitigated FVG in the leg")
            return None

        # --- Step 4: entry ----------------------------------------------
        entry = round_price(fvg.entry_for(cfg.fvg_entry_mode), self.point_size)
        spread = snapshot.quote.spread
        if sweep.direction is Direction.SELL and entry <= snapshot.quote.ask + self.points(1):
            result.reject(f"Sell entry {entry:.2f} is not above the ask {snapshot.quote.ask:.2f}")
            return None
        if sweep.direction is Direction.BUY and entry >= snapshot.quote.bid - self.points(1):
            result.reject(f"Buy entry {entry:.2f} is not below the bid {snapshot.quote.bid:.2f}")
            return None

        # --- Step 5a: structural stop -----------------------------------
        extreme = leg_extreme(candles, mss)
        buffer = self.points(cfg.sl_buffer_points)
        if sweep.direction is Direction.SELL:
            stop_loss = round_price(max(extreme, sweep.extreme) + buffer, self.point_size)
        else:
            stop_loss = round_price(min(extreme, sweep.extreme) - buffer, self.point_size)

        stop_distance = abs(entry - stop_loss)
        if stop_distance <= 0:
            result.reject("Computed a zero-width stop; refusing the setup")
            return None
        if stop_distance < spread * 2:
            result.reject(
                f"Stop distance {self.to_points(stop_distance):.0f}pt is inside twice the "
                f"spread ({self.to_points(spread):.0f}pt)"
            )
            return None

        # --- Step 5b: draw on liquidity ---------------------------------
        target = draw_on_liquidity(
            pools,
            sweep.direction,
            entry,
            stop_loss,
            min_rr=cfg.min_rr,
            min_distance=self.points(cfg.min_sweep_penetration_points),
        )
        if target is None:
            result.reject(
                f"No draw-on-liquidity target pays the {cfg.min_rr:.1f}R minimum "
                f"(stop {self.to_points(stop_distance):.0f}pt)"
            )
            return None
        pool, risk_reward = target
        take_profit = round_price(pool.price, self.point_size)

        narrative = [
            f"Swept {sweep.pool.name} @ {sweep.pool.price:.2f} "
            f"({self.to_points(sweep.penetration):.0f}pt raid, {sweep.ts:%H:%M} UTC)",
            f"MSS through {mss.broken_level:.2f} @ {mss.ts:%H:%M} "
            f"({self.to_points(mss.displacement_points):.0f}pt displacement)",
            f"{'Bearish' if sweep.direction is Direction.SELL else 'Bullish'} FVG "
            f"{fvg.bottom:.2f}-{fvg.top:.2f} ({self.to_points(fvg.size):.0f}pt)",
            f"Entry {entry:.2f} ({cfg.fvg_entry_mode} edge) | "
            f"SL {stop_loss:.2f} ({self.to_points(stop_distance):.0f}pt) | "
            f"TP {take_profit:.2f} -> {pool.name}",
            f"Risk/Reward {risk_reward:.2f}R",
        ]

        return TradeSetup(
            direction=sweep.direction,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            risk_reward=risk_reward,
            sweep=sweep,
            mss=mss,
            fvg=fvg,
            target=pool,
            window=snapshot.window_name,
            narrative=narrative,
        )

    # -- internals ---------------------------------------------------------

    def _select_fvg(
        self,
        candles: Sequence[Candle],
        snapshot: MarketSnapshot,
        mss,
        price_now: float,
    ) -> Optional[FairValueGap]:
        """FVG from the execution timeframe, falling back to the structure TF.

        The displacement leg is often only three M1 candles wide, which can leave
        no qualifying gap; the same move on M5 frequently does show one, and ICT
        treats both as valid Silver Bullet entries.
        """
        min_size = self.points(self._cfg.min_fvg_points)
        gaps = find_fvgs(
            candles,
            mss.direction,
            start_index=max(1, mss.leg_start),
            end_index=mss.leg_end,
            min_size=min_size,
        )
        chosen = select_entry_fvg(
            candles, gaps, entry_mode=self._cfg.fvg_entry_mode, current_price=price_now
        )
        if chosen is not None:
            return chosen

        structure = list(snapshot.structure_candles or [])
        if not structure:
            return None
        leg_start_ts = candles[mss.leg_start].ts
        leg_end_ts = candles[mss.leg_end].ts
        indices = [i for i, c in enumerate(structure) if leg_start_ts <= c.ts <= leg_end_ts]
        if len(indices) < 3:
            return None
        gaps = find_fvgs(
            structure,
            mss.direction,
            start_index=max(1, indices[0]),
            end_index=indices[-1],
            min_size=min_size,
        )
        return select_entry_fvg(
            structure, gaps, entry_mode=self._cfg.fvg_entry_mode, current_price=price_now
        )

    def _htf_gap_pools(self, htf_candles: Sequence[Candle]) -> list[LiquidityPool]:
        """Unfilled HTF gaps expressed as draw-on-liquidity pools."""
        pools: list[LiquidityPool] = []
        for gap in htf_unfilled_gaps(htf_candles, min_size=self.points(self._cfg.min_fvg_points * 2)):
            # A bearish HTF gap sits above price and draws it up (buy-side draw);
            # a bullish one sits below and draws price down.
            side = PoolSide.BUYSIDE if gap.direction is Direction.SELL else PoolSide.SELLSIDE
            pools.append(LiquidityPool("HTF_FVG", gap.mid, side, gap.ts, sweepable=False))
        return pools
