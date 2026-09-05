"""Timeframe vocabulary.

The bot thinks in canonical names ("M1", "M5", "H1").  The upstream MCP server
speaks its own dialect - the verified cTrader proxy uses ``M_1``/``H_1`` - so we
carry a list of aliases per timeframe and let the schema binder pick whichever
one the live ``tools/list`` enum actually advertises.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Timeframe:
    name: str
    seconds: int
    aliases: tuple[str, ...]

    @property
    def minutes(self) -> float:
        return self.seconds / 60.0


def _tf(name: str, seconds: int, *extra: str) -> Timeframe:
    # Aliases are ordered best-first; "M_1" is the verified cTrader spelling.
    base = (name.replace("M", "M_", 1) if name.startswith("M") and not name.startswith("MN")
            else name.replace("H", "H_", 1) if name.startswith("H")
            else name.replace("D", "D_", 1) if name.startswith("D")
            else name.replace("W", "W_", 1) if name.startswith("W")
            else name)
    return Timeframe(name, seconds, tuple(dict.fromkeys((base, name, *extra))))


TIMEFRAMES: dict[str, Timeframe] = {
    tf.name: tf
    for tf in (
        _tf("M1", 60, "MINUTE_1", "MINUTE", "1m", "1"),
        _tf("M5", 300, "MINUTE_5", "5m", "5"),
        _tf("M15", 900, "MINUTE_15", "15m", "15"),
        _tf("M30", 1800, "MINUTE_30", "30m", "30"),
        _tf("H1", 3600, "HOUR_1", "HOUR", "1h", "60"),
        _tf("H4", 14400, "HOUR_4", "4h", "240"),
        _tf("D1", 86400, "DAY_1", "DAILY", "1d"),
        _tf("W1", 604800, "WEEK_1", "WEEKLY", "1w"),
        Timeframe("MN1", 2592000, ("MN_1", "MN1", "MONTH_1", "MONTHLY")),
    )
}


def get_timeframe(name: str) -> Timeframe:
    key = name.strip().upper().replace("_", "")
    if key in TIMEFRAMES:
        return TIMEFRAMES[key]
    raise KeyError(f"Unknown timeframe {name!r}; known: {sorted(TIMEFRAMES)}")
