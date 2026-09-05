"""Position sizing from a fixed fractional risk and the *actual* stop distance.

The stop is structural, so the lot size has to be derived per trade: risking a
fixed 1% behind a 60-point stop and behind a 300-point stop are completely
different position sizes.  Rounding is always **down** onto the broker's volume
step - rounding up would quietly breach the risk limit on every trade.
"""

from __future__ import annotations

from dataclasses import dataclass

from utils.logging import get_logger
from utils.prices import round_to_step

log = get_logger("risk.sizing")


@dataclass(frozen=True)
class SizingResult:
    lots: float
    risk_amount: float        # the money we intended to risk
    actual_risk: float        # what the rounded lot size really risks
    risk_per_lot: float
    accepted: bool
    reason: str = ""

    @property
    def risk_pct_of(self) -> float:
        return self.actual_risk


def calculate_position_size(
    *,
    balance: float,
    risk_pct: float,
    stop_distance: float,
    contract_size: float,
    volume_step: float,
    min_volume: float,
    max_volume: float,
) -> SizingResult:
    """Lots to trade for ``risk_pct`` of ``balance`` behind ``stop_distance``.

    ``stop_distance`` is in price units (e.g. 11.50 on gold) and
    ``contract_size`` is units per lot (100 oz), so one lot loses
    ``stop_distance * contract_size`` quote-currency units if the stop is hit.
    """
    if balance <= 0:
        return SizingResult(0.0, 0.0, 0.0, 0.0, False, f"Balance is {balance:.2f}")
    if stop_distance <= 0:
        return SizingResult(0.0, 0.0, 0.0, 0.0, False, "Stop distance is zero")
    if contract_size <= 0:
        return SizingResult(0.0, 0.0, 0.0, 0.0, False, "Contract size is not configured")

    risk_amount = balance * (risk_pct / 100.0)
    risk_per_lot = stop_distance * contract_size
    raw_lots = risk_amount / risk_per_lot
    lots = round_to_step(raw_lots, volume_step, mode="down")

    if lots < min_volume:
        return SizingResult(
            0.0, risk_amount, 0.0, risk_per_lot, False,
            f"Required size {raw_lots:.4f} lots is below the {min_volume} minimum - "
            f"taking the minimum would risk "
            f"{min_volume * risk_per_lot:.2f} ({min_volume * risk_per_lot / balance * 100:.2f}% "
            f"of balance) instead of the permitted {risk_pct:.2f}%",
        )

    note = ""
    if lots > max_volume:
        lots = round_to_step(max_volume, volume_step, mode="down")
        note = f"clamped to the {max_volume} lot maximum"

    actual_risk = lots * risk_per_lot
    return SizingResult(lots, risk_amount, actual_risk, risk_per_lot, True, note)
