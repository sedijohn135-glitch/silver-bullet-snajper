"""Synthetic market fixtures used by the strategy tests.

The scenario is a textbook afternoon Silver Bullet on gold: London's high is
raided, price displaces down through short-term structure leaving a clean bearish
FVG, and the previous-day low sits far enough below to pay better than 2R.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from models import Candle, Quote

UTC = timezone.utc
# 2026-09-04 is a Friday; Tirane is UTC+2 (CEST), so the 16:00-17:00 local
# AFTERNOON window is 14:00-15:00 UTC.
DAY = datetime(2026, 9, 4, tzinfo=UTC)
WINDOW_START = DAY.replace(hour=14)
NOW = DAY.replace(hour=14, minute=10)


def _candle(ts: datetime, o: float, h: float, l: float, c: float, v: float = 100.0) -> Candle:
    return Candle(ts=ts, open=o, high=h, low=l, close=c, volume=v)


def drift(start_ts: datetime, count: int, period: int, base: float,
          amplitude: float = 0.4, slope: float = 0.0) -> list[Candle]:
    """Low-volatility filler bars - small bodies so displacement stands out."""
    out: list[Candle] = []
    for i in range(count):
        centre = base + slope * i + amplitude * ((i % 5) - 2) / 2.0
        o = centre - 0.05
        c = centre + 0.05
        out.append(_candle(start_ts + timedelta(seconds=period * i),
                           o, max(o, c) + 0.15, min(o, c) - 0.15, c))
    return out


def m5_history() -> list[Candle]:
    """00:00-14:00 UTC of M5 bars covering the Asian and London sessions."""
    bars: list[Candle] = []
    # Asia 00:00-06:00 UTC (02:00-08:00 Tirane): range 4400 - 4420.
    bars += drift(DAY, 71, 300, 4410.0, amplitude=0.6)
    bars.append(_candle(DAY + timedelta(minutes=350), 4412.0, 4420.0, 4411.0, 4415.0))
    bars.append(_candle(DAY + timedelta(minutes=355), 4415.0, 4416.0, 4400.0, 4408.0))
    # Gap to London 07:00-11:00 UTC (09:00-13:00 Tirane): range 4410 - 4430.
    london_start = DAY.replace(hour=7)
    bars += drift(london_start, 20, 300, 4418.0, amplitude=0.8)
    bars.append(_candle(london_start + timedelta(minutes=100), 4420.0, 4430.0, 4419.0, 4427.0))
    bars += drift(london_start + timedelta(minutes=105), 20, 300, 4424.0, amplitude=0.7)
    bars.append(_candle(london_start + timedelta(minutes=205), 4423.0, 4424.0, 4410.0, 4416.0))
    bars += drift(london_start + timedelta(minutes=210), 6, 300, 4420.0)
    # Midday lull 11:00-14:00 UTC.
    bars += drift(DAY.replace(hour=11), 36, 300, 4425.0, amplitude=0.5)
    return sorted(bars, key=lambda c: c.ts)


def h1_history() -> list[Candle]:
    """Two days of H1 bars; the previous day printed its low at 4392."""
    bars: list[Candle] = []
    prev = DAY - timedelta(days=1)
    bars += drift(prev, 12, 3600, 4405.0, amplitude=1.5)
    bars.append(_candle(prev + timedelta(hours=12), 4404.0, 4406.0, 4392.0, 4398.0))
    bars += drift(prev + timedelta(hours=13), 11, 3600, 4404.0, amplitude=1.5)
    bars += drift(DAY, 14, 3600, 4418.0, amplitude=2.0)
    return sorted(bars, key=lambda c: c.ts)


#: (open, high, low, close) for each M1 bar from 13:52 UTC onwards.
_SEQUENCE: list[tuple[float, float, float, float]] = [
    # 13:52-13:59 - consolidation that prints the swing low at 4422.00
    (4425.0, 4425.4, 4424.6, 4424.9),
    (4424.9, 4425.2, 4424.3, 4424.5),
    (4424.5, 4424.7, 4422.00, 4423.2),   # swing low 4422.00
    (4423.2, 4424.0, 4423.0, 4423.9),
    (4423.9, 4424.6, 4423.6, 4424.4),
    (4424.4, 4425.6, 4424.2, 4425.4),
    (4425.4, 4426.4, 4425.1, 4426.2),
    (4426.2, 4427.4, 4426.0, 4427.2),
    # 14:00-14:01 - drive into the London high at 4430
    (4427.2, 4428.6, 4427.0, 4428.4),
    (4428.4, 4429.6, 4428.2, 4429.4),
    # 14:02 - THE SWEEP: takes 4430 by 80 points, closes back below
    (4429.4, 4430.80, 4428.30, 4428.50),
    # 14:03-14:04 - displacement down; MSS closes through 4422.00
    (4428.5, 4428.80, 4425.00, 4425.20),
    (4425.2, 4425.30, 4418.00, 4418.50),   # FVG middle candle
    # 14:05 - third candle of the gap: high 4419.50 < 4425.00 => bearish FVG
    (4418.5, 4419.50, 4416.00, 4417.00),
    # 14:06-14:09 - drifts sideways; the gap stays unmitigated
    (4417.0, 4418.60, 4416.40, 4418.10),
    (4418.1, 4419.40, 4417.30, 4418.00),
    (4418.0, 4418.90, 4417.10, 4417.60),
    (4417.6, 4418.70, 4417.00, 4418.20),
]


def m1_series() -> list[Candle]:
    """M1 bars: 90 minutes of context, then the scripted setup."""
    warmup = drift(DAY.replace(hour=12, minute=22), 90, 60, 4425.0, amplitude=0.35)
    start = DAY.replace(hour=13, minute=52)
    scripted = [
        _candle(start + timedelta(minutes=i), *ohlc)
        for i, ohlc in enumerate(_SEQUENCE)
    ]
    return warmup + scripted


def quote(bid: float = 4418.10, ask: float = 4418.40) -> Quote:
    """A 30-point spread - inside the 35-point guard."""
    return Quote(symbol_id=41, bid=bid, ask=ask, ts=NOW)
