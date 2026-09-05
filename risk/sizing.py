"""Position sizing from a fixed fractional risk and the *actual* stop distance.

The stop is structural, so the lot size has to be derived per trade: risking a
fixed 1% behind a 60-point stop and behind a 300-point stop are completely
different position sizes.  Rounding is always **down** onto the broker's volume
step - rounding up would quietly breach the risk limit on every trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from utils.logging import get_logger
from utils.prices import round_to_step

log = get_logger("risk.sizing")


@dataclass(frozen=True)
class SizingResult:
    lots: float               # TOTAL size across every layer
    risk_amount: float        # the money we intended to risk
    actual_risk: float        # what the rounded total really risks
    risk_per_lot: float
    accepted: bool
    reason: str = ""
    risk_pct: float = 0.0     # actual_risk as a percentage of balance
    fixed: bool = False       # True when the operator pinned the lot size
    layers: int = 1           # how many limit orders the entry is split into
    lots_per_layer: float = 0.0


def calculate_position_size(
    *,
    balance: float,
    risk_pct: float,
    stop_distance: float,
    contract_size: float,
    volume_step: float,
    min_volume: float,
    max_volume: float,
    entry_price: Optional[float] = None,
    max_notional_leverage: float = 0.0,
    fixed_lots: float = 0.0,
    max_risk_pct: float = 0.0,
    layers: int = 1,
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

    # --- operator-pinned lot size ---------------------------------------
    # When FIXED_LOT_SIZE is set the position size stops being derived from
    # risk. That is a deliberate trade-off: it guarantees the bot can act on a
    # setup that percentage sizing would have refused, at the cost of a risk
    # that now varies with the stop distance. The real figure is computed and
    # reported on every order so it is never a surprise.
    layers = max(1, int(layers))

    if fixed_lots > 0:
        per_layer = min(max(round_to_step(fixed_lots, volume_step, mode="down"), min_volume),
                        max_volume)
        used_layers = layers
        # The total across the ladder still has to fit the broker's ceiling.
        while used_layers > 1 and per_layer * used_layers > max_volume:
            used_layers -= 1
        lots = per_layer * used_layers
        actual = lots * risk_per_lot
        pct = actual / balance * 100.0
        ladder = f"{used_layers}x{per_layer}" if used_layers > 1 else f"{per_layer}"
        if max_risk_pct > 0 and pct > max_risk_pct:
            return SizingResult(
                0.0, risk_amount, actual, risk_per_lot, False,
                f"Fixed {ladder} lots behind a {stop_distance:.2f} stop would risk "
                f"{actual:.2f} ({pct:.2f}% of {balance:.2f}), above the "
                f"{max_risk_pct:.2f}% ceiling - raise MAX_RISK_PCT, lower FIXED_LOT_SIZE, "
                f"or use fewer ENTRY_LAYERS",
                risk_pct=pct, fixed=True, layers=used_layers, lots_per_layer=per_layer,
            )
        return SizingResult(lots, risk_amount, actual, risk_per_lot, True,
                            f"fixed size {ladder} ({pct:.2f}% of balance at risk)",
                            risk_pct=pct, fixed=True,
                            layers=used_layers, lots_per_layer=per_layer)

    # --- risk-derived sizing, split across the ladder --------------------
    raw_lots = min(risk_amount / risk_per_lot, max_volume)
    used_layers = layers
    per_layer = round_to_step(raw_lots / used_layers, volume_step, mode="down")
    # A ladder is only worth having if each rung clears the broker minimum;
    # otherwise take fewer, larger rungs rather than refusing the setup.
    while per_layer < min_volume and used_layers > 1:
        used_layers -= 1
        per_layer = round_to_step(raw_lots / used_layers, volume_step, mode="down")

    if per_layer < min_volume:
        return SizingResult(
            0.0, risk_amount, 0.0, risk_per_lot, False,
            f"Required size {raw_lots:.4f} lots is below the {min_volume} minimum - "
            f"taking the minimum would risk "
            f"{min_volume * risk_per_lot:.2f} ({min_volume * risk_per_lot / balance * 100:.2f}% "
            f"of balance) instead of the permitted {risk_pct:.2f}%. "
            f"Set FIXED_LOT_SIZE to take the trade at a size you choose instead.",
        )

    lots = per_layer * used_layers
    note = f"{used_layers} layers of {per_layer}" if used_layers > 1 else ""
    if used_layers < layers:
        note += f" (reduced from {layers}: each rung must clear the {min_volume} minimum)"

    # Notional sanity check - a backstop against a *scale* failure.
    #
    # If price-scale auto-detection ever picks the wrong divisor, entry_price
    # arrives as 8,006,716,000 instead of 80,067 and every other number in this
    # function still looks perfectly reasonable. Position value is the one place
    # that error becomes obvious, so it is checked before the order is built.
    #
    # It does NOT validate contract_size: that value is what the check is
    # computed *from*, so a subtly wrong one passes. The symbol listing does not
    # publish contract size, so it must be confirmed against the broker's own
    # symbol specification - the start-up log prints the value in use.
    if max_notional_leverage > 0 and entry_price:
        notional = lots * contract_size * entry_price
        if notional > balance * max_notional_leverage:
            return SizingResult(
                0.0, risk_amount, 0.0, risk_per_lot, False,
                f"Position notional {notional:,.0f} is {notional / balance:.0f}x the "
                f"{balance:,.2f} balance, above the {max_notional_leverage:.0f}x ceiling - "
                f"check CONTRACT_SIZE for this symbol before trading it",
            )

    actual_risk = lots * risk_per_lot
    return SizingResult(lots, risk_amount, actual_risk, risk_per_lot, True, note,
                        risk_pct=actual_risk / balance * 100.0,
                        layers=used_layers, lots_per_layer=per_layer)
