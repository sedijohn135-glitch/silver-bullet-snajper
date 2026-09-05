"""Strategy-domain types for the ICT Silver Bullet setup."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "Direction":
        return Direction.SELL if self is Direction.BUY else Direction.BUY

    @property
    def sign(self) -> int:
        return 1 if self is Direction.BUY else -1


class PoolSide(str, Enum):
    """Which side of the book the resting liquidity sits on.

    BUYSIDE pools sit *above* price (stops of shorts, breakout buy orders);
    sweeping one is the trigger for a short setup, and untouched ones are the
    draw-on-liquidity target for a long.
    """

    BUYSIDE = "BUYSIDE"
    SELLSIDE = "SELLSIDE"


@dataclass(frozen=True)
class SwingPoint:
    index: int
    ts: datetime
    price: float
    is_high: bool


@dataclass(frozen=True)
class LiquidityPool:
    """A price level where stop orders are expected to rest."""

    name: str            # e.g. "ASIA_HIGH", "EQL", "PDL", "HTF_FVG"
    price: float
    side: PoolSide
    ts: Optional[datetime] = None
    strength: int = 1    # equal highs/lows touched N times are stronger
    sweepable: bool = True
    """Whether a raid of this level can *trigger* a setup.

    Session highs/lows and equal highs/lows hold real resting stops, so taking
    them out is a genuine liquidity raid.  An unfilled higher-timeframe FVG is a
    magnet, not a stop cluster: it belongs in the take-profit target list only,
    and treating it as a trigger fires the strategy on ordinary drift.
    """

    def __str__(self) -> str:  # pragma: no cover - logging sugar
        return f"{self.name}@{self.price:.2f}"


@dataclass(frozen=True)
class Sweep:
    """A raid of resting liquidity that then rejected back through the level."""

    pool: LiquidityPool
    index: int               # candle that made the extreme
    ts: datetime
    extreme: float           # highest high / lowest low of the raid
    close_back_index: int    # candle that closed back inside the range
    direction: Direction     # the trade direction this sweep implies

    @property
    def penetration(self) -> float:
        return abs(self.extreme - self.pool.price)


@dataclass(frozen=True)
class MarketStructureShift:
    """Displacement through prior structure - the confirmation leg."""

    index: int               # candle whose close broke structure
    ts: datetime
    broken_level: float
    leg_start: int           # sweep candle index
    leg_end: int             # MSS candle index
    displacement_index: int  # strongest body in the leg
    displacement_points: float
    direction: Direction


@dataclass(frozen=True)
class FairValueGap:
    """A 3-candle imbalance left behind by the displacement leg."""

    direction: Direction     # trade direction this FVG serves
    top: float
    bottom: float
    first_index: int
    middle_index: int
    third_index: int
    ts: datetime

    @property
    def size(self) -> float:
        return self.top - self.bottom

    @property
    def mid(self) -> float:
        """Consequent encroachment - the 50% level of the gap."""
        return (self.top + self.bottom) / 2.0

    @property
    def proximal(self) -> float:
        """The edge price reaches first when returning to the gap."""
        return self.bottom if self.direction is Direction.SELL else self.top

    @property
    def distal(self) -> float:
        """The far edge - a full fill of the imbalance."""
        return self.top if self.direction is Direction.SELL else self.bottom

    def entry_for(self, mode: str) -> float:
        if mode == "mid":
            return self.mid
        if mode == "distal":
            return self.distal
        return self.proximal


@dataclass(frozen=True)
class TradeSetup:
    """A fully specified, risk-defined trade idea."""

    direction: Direction
    entry: float
    stop_loss: float
    take_profit: float
    risk_reward: float
    # Optional so a boot-time preflight can build a representative order
    # without inventing a fake sweep/MSS/FVG chain to go with it.
    sweep: Optional[Sweep]
    mss: Optional[MarketStructureShift]
    fvg: Optional[FairValueGap]
    target: Optional[LiquidityPool]
    window: str
    narrative: list[str] = field(default_factory=list)

    @property
    def stop_distance(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def target_distance(self) -> float:
        return abs(self.take_profit - self.entry)


@dataclass
class AnalysisResult:
    """Outcome of one analysis pass - a setup, or the reasons there wasn't one."""

    setup: Optional[TradeSetup] = None
    rejections: list[str] = field(default_factory=list)
    pools_considered: int = 0
    candles_used: int = 0

    def reject(self, reason: str) -> "AnalysisResult":
        """Record why a candidate failed, without repeating an identical reason.

        Several sweep candidates commonly fail the same way ("no unmitigated FVG
        in the leg"), and eight copies of one sentence tells the reader nothing
        the first copy did not.
        """
        if reason not in self.rejections:
            self.rejections.append(reason)
        return self

    @property
    def found(self) -> bool:
        return self.setup is not None
