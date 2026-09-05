"""Per-symbol trading profiles and the weekday/weekend schedule.

Every threshold in this bot is expressed in **points**, and a point means wildly
different things per instrument.  Measured on the live feed:

* XAUUSD ~4,431 - median M1 body ~35 points, spread ~40 points.
* BTCUSD ~80,067 - median M1 body ~2,960 points, spread ~500 points.

So gold's 35-point spread guard would reject *every* BTC trade, and gold's
100 oz/lot contract size would size a BTC position 100x too large. A single
global set of numbers cannot serve both; each symbol carries its own profile.

The schedule exists because the two markets keep different hours: XAUUSD is shut
from Friday evening to Sunday evening, while BTCUSD trades continuously. Trading
gold on a Saturday is a no-op; the weekend belongs to crypto.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import date
from typing import Optional

from utils.logging import get_logger

log = get_logger("symbols")

#: Python's date.weekday(): Monday=0 ... Saturday=5, Sunday=6.
WEEKEND_DAYS = frozenset({5, 6})


@dataclass(frozen=True)
class SymbolProfile:
    """Everything that differs between one tradeable instrument and another."""

    name: str

    # --- contract mechanics ---------------------------------------------
    contract_size: float      # units of the base asset per 1.00 lot
    point_size: float         # smallest price increment
    volume_step: float
    min_volume: float
    max_volume: float

    # --- point-based thresholds -----------------------------------------
    max_spread_points: float
    sl_buffer_points: float
    min_fvg_points: float
    min_displacement_points: float
    min_sweep_penetration_points: float
    equal_level_tolerance_points: float

    # --- price-scale auto-detection band --------------------------------
    sane_price_min: float
    sane_price_max: float

    def points_to_price(self, points: float) -> float:
        return points * self.point_size

    def price_to_points(self, price_delta: float) -> float:
        return price_delta / self.point_size if self.point_size else 0.0

    def describe(self) -> str:
        return (
            f"{self.name}: point={self.point_size} contract={self.contract_size} "
            f"spread<={self.max_spread_points:.0f}pt sl_buffer={self.sl_buffer_points:.0f}pt "
            f"min_fvg={self.min_fvg_points:.0f}pt vol[{self.min_volume}..{self.max_volume}"
            f"/{self.volume_step}]"
        )


#: Gold. The point-based values are the ones from the original specification:
#: a 20-point (2-pip) structural stop buffer and a 35-point (3.5-pip) spread cap.
XAUUSD = SymbolProfile(
    name="XAUUSD",
    contract_size=100.0,          # 100 troy ounces per lot
    point_size=0.01,              # 1 pip = 10 points
    volume_step=0.01,
    min_volume=0.01,
    max_volume=100.0,
    max_spread_points=35.0,
    sl_buffer_points=20.0,
    min_fvg_points=8.0,
    min_displacement_points=30.0,
    min_sweep_penetration_points=3.0,
    equal_level_tolerance_points=15.0,
    sane_price_min=100.0,
    sane_price_max=100_000.0,
)

#: Bitcoin. Thresholds derived from M1/M5 bars pulled from the live server at
#: ~80,067: median M1 body 2,960 points, median M1 range 5,413 points, spread
#: 500 points. Each value keeps the same *relative* meaning it has for gold.
BTCUSD = SymbolProfile(
    name="BTCUSD",
    contract_size=1.0,            # 1 BTC per lot - CONFIRM in cTrader symbol specs
    point_size=0.01,
    volume_step=0.01,
    min_volume=0.01,
    max_volume=10.0,
    max_spread_points=700.0,      # live spread was 500pt; this allows normal widening
    sl_buffer_points=1000.0,      # 10 USD beyond the sweep wick
    min_fvg_points=400.0,         # 4 USD - a gap smaller than this is noise here
    min_displacement_points=2000.0,   # 20 USD ~ one median M1 body
    min_sweep_penetration_points=200.0,
    equal_level_tolerance_points=1000.0,
    sane_price_min=1_000.0,
    sane_price_max=1_000_000.0,
)

BUILTIN_PROFILES: dict[str, SymbolProfile] = {p.name: p for p in (XAUUSD, BTCUSD)}

#: Profile fields an operator may override per symbol via `<SYMBOL>_<FIELD>` env
#: vars, e.g. `BTCUSD_CONTRACT_SIZE=1` or `BTCUSD_MAX_SPREAD_POINTS=900`.
OVERRIDABLE = tuple(f.name for f in fields(SymbolProfile) if f.name != "name")


def profile_for(name: str) -> SymbolProfile:
    """Built-in profile for ``name``, or a gold-shaped one as a last resort."""
    key = name.strip().upper()
    if key in BUILTIN_PROFILES:
        return BUILTIN_PROFILES[key]
    log.warning(
        "No built-in profile for %s; falling back to XAUUSD's numbers. Point-based "
        "thresholds and CONTRACT_SIZE are almost certainly wrong - set %s_* overrides.",
        key, key,
    )
    return replace(XAUUSD, name=key)


def apply_overrides(profile: SymbolProfile, overrides: dict[str, float]) -> SymbolProfile:
    """Return ``profile`` with the given field overrides applied."""
    clean = {k: v for k, v in overrides.items() if k in OVERRIDABLE and v is not None}
    return replace(profile, **clean) if clean else profile


def symbol_for_day(
    day: date, *, weekday_symbol: str, weekend_symbol: str, weekend_enabled: bool
) -> Optional[str]:
    """Which instrument to trade on ``day``, or None if the bot should idle.

    ``day`` must be the *local* (Europe/Tirane) date, since the windows are
    defined in local time and a Saturday window is a Saturday window regardless
    of what UTC thinks.
    """
    if day.weekday() in WEEKEND_DAYS:
        return weekend_symbol if (weekend_enabled and weekend_symbol) else None
    return weekday_symbol or None
