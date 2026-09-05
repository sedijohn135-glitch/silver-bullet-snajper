"""Runtime state, with best-effort persistence.

Railway containers are ephemeral, so the broker is always the source of truth for
anything that matters (open orders, realised P&L).  This file exists purely to
avoid *cosmetic* duplication across a redeploy - e.g. re-sending a Telegram alert
for a position that was already reported - and to expose a status snapshot.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from utils.logging import get_logger

log = get_logger("engine.state")

STATE_FILE = "bot_state.json"


@dataclass
class BotState:
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    last_tick: Optional[datetime] = None
    last_analysis: Optional[datetime] = None
    last_analysis_window: str = ""
    last_rejections: list[str] = field(default_factory=list)
    executed_windows: dict[str, str] = field(default_factory=dict)   # window label -> ISO ts
    known_position_ids: set[str] = field(default_factory=set)
    known_order_ids: set[str] = field(default_factory=set)
    reported_closures: set[str] = field(default_factory=set)
    trades_today: int = 0
    halted_reason: str = ""
    last_error: str = ""

    # -- persistence -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "executed_windows": self.executed_windows,
            "known_position_ids": sorted(self.known_position_ids),
            "known_order_ids": sorted(self.known_order_ids),
            "reported_closures": sorted(self.reported_closures)[-200:],
            "trades_today": self.trades_today,
        }

    def apply(self, data: dict[str, Any]) -> None:
        self.executed_windows = dict(data.get("executed_windows") or {})
        self.known_position_ids = set(data.get("known_position_ids") or [])
        self.known_order_ids = set(data.get("known_order_ids") or [])
        self.reported_closures = set(data.get("reported_closures") or [])
        self.trades_today = int(data.get("trades_today") or 0)

    def snapshot(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "last_tick": self.last_tick.isoformat() if self.last_tick else None,
            "last_analysis": self.last_analysis.isoformat() if self.last_analysis else None,
            "last_analysis_window": self.last_analysis_window,
            "last_rejections": self.last_rejections[-5:],
            "executed_windows": self.executed_windows,
            "open_positions_tracked": len(self.known_position_ids),
            "pending_orders_tracked": len(self.known_order_ids),
            "trades_today": self.trades_today,
            "halted_reason": self.halted_reason,
            "last_error": self.last_error,
        }


class StateStore:
    def __init__(self, directory: str) -> None:
        self._path = os.path.join(directory, STATE_FILE)
        self._directory = directory

    def load(self, state: BotState) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                state.apply(json.load(handle))
            log.info("Restored local state from %s", self._path)
        except FileNotFoundError:
            log.debug("No local state file at %s (fresh container)", self._path)
        except Exception as exc:  # noqa: BLE001 - state is a convenience, not a dependency
            log.warning("Could not read state file %s: %s", self._path, exc)

    def save(self, state: BotState) -> None:
        try:
            os.makedirs(self._directory, exist_ok=True)
            tmp = f"{self._path}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(state.to_dict(), handle, indent=2)
            os.replace(tmp, self._path)   # atomic; never leaves a half-written file
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not persist state: %s", exc)
