"""Trading-clock helpers, all anchored to Albania local time.

Two distinct concepts live here:

* **Silver Bullet windows** - the three one-hour slots in which the bot is
  allowed to execute (09-10, 16-17, 20-21 Europe/Tirane).
* **Liquidity sessions** - Asia / London / New York AM ranges whose highs and
  lows form the liquidity pools the Silver Bullet setup hunts.

``ZoneInfo`` is used deliberately instead of a fixed UTC offset: Albania observes
CET/CEST, so a hardcoded +2 would silently shift every window by an hour for
roughly half the year.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from config import LOCAL_TZ_NAME

LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
UTC = timezone.utc


@dataclass(frozen=True)
class TimeWindow:
    """A named daily window expressed in local wall-clock time."""

    name: str
    start: time
    end: time

    def bounds_on(self, day: date) -> tuple[datetime, datetime]:
        """Concrete tz-aware [start, end) datetimes for ``day`` in local time."""
        start = datetime.combine(day, self.start, tzinfo=LOCAL_TZ)
        end = datetime.combine(day, self.end, tzinfo=LOCAL_TZ)
        if end <= start:  # window wraps past midnight (not used today, but safe)
            end += timedelta(days=1)
        return start, end

    def contains(self, moment: datetime) -> bool:
        local = to_local(moment)
        start, end = self.bounds_on(local.date())
        return start <= local < end


#: The three Silver Bullet execution windows (Albania local time).
#: They map onto the classic ICT sessions: 09-10 = London open SB,
#: 16-17 = New York AM SB (10:00-11:00 ET), 20-21 = New York PM SB (14:00-15:00 ET).
SILVER_BULLET_WINDOWS: tuple[TimeWindow, ...] = (
    TimeWindow("MORNING", time(9, 0), time(10, 0)),
    TimeWindow("AFTERNOON", time(16, 0), time(17, 0)),
    TimeWindow("EVENING", time(20, 0), time(21, 0)),
)

#: Liquidity-building sessions, also in Albania local time.
LIQUIDITY_SESSIONS: tuple[TimeWindow, ...] = (
    TimeWindow("ASIA", time(2, 0), time(8, 0)),
    TimeWindow("LONDON", time(9, 0), time(13, 0)),
    TimeWindow("NEWYORK_AM", time(15, 30), time(18, 0)),
)


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def to_local(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(LOCAL_TZ)


def to_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=LOCAL_TZ)
    return moment.astimezone(UTC)


def active_window(moment: Optional[datetime] = None) -> Optional[TimeWindow]:
    """The Silver Bullet window containing ``moment``, or None."""
    moment = moment or now_utc()
    for window in SILVER_BULLET_WINDOWS:
        if window.contains(moment):
            return window
    return None


def next_window_start(moment: Optional[datetime] = None) -> datetime:
    """UTC datetime at which the next Silver Bullet window opens."""
    local = to_local(moment or now_utc())
    candidates: list[datetime] = []
    for day_offset in (0, 1):
        day = local.date() + timedelta(days=day_offset)
        for window in SILVER_BULLET_WINDOWS:
            start, _ = window.bounds_on(day)
            if start > local:
                candidates.append(start)
    return to_utc(min(candidates))


def window_key(window: TimeWindow, moment: Optional[datetime] = None) -> str:
    """Stable identifier for "this window, on this local day".

    Used both as the per-window execution lock and as the order label, so the
    one-trade-per-window rule survives a Railway redeploy: the label is read back
    off the broker's own open orders rather than trusted from memory.
    """
    local = to_local(moment or now_utc())
    return f"{local.strftime('%Y%m%d')}-{window.name}"


def local_day_start_utc(moment: Optional[datetime] = None) -> datetime:
    """Midnight (local) of the day containing ``moment``, as UTC."""
    local = to_local(moment or now_utc())
    midnight = datetime.combine(local.date(), time(0, 0), tzinfo=LOCAL_TZ)
    return to_utc(midnight)


def session_ranges_before(
    moment: datetime, sessions: tuple[TimeWindow, ...] = LIQUIDITY_SESSIONS
) -> list[tuple[TimeWindow, datetime, datetime]]:
    """Liquidity sessions of the current local day that have already started.

    A session that is still running is included but truncated at ``moment`` -
    its running high/low is live liquidity just the same.
    """
    local = to_local(moment)
    out: list[tuple[TimeWindow, datetime, datetime]] = []
    for session in sessions:
        start, end = session.bounds_on(local.date())
        if start >= local:
            continue  # hasn't started yet today
        out.append((session, to_utc(start), to_utc(min(end, local))))
    return out
