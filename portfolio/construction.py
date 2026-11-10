"""Weights become quantities here — the line in the codebase where
``float`` signal maths (design rule 4) turns into ``Decimal`` money.

Everything upstream of ``target_quantity`` (strategy weights, vol
targeting, buffering) is float because it's signal maths. Everything
from here down (quantities, orders, fills) is ``Decimal`` because it's
actually moving money and has to round to real exchange constraints —
tick size, step size, minimum notional (design rule 4).
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal


def round_to_step(quantity: Decimal, step_size: Decimal) -> Decimal:
    """Round *down* to the instrument's step size.

    Down, not to-nearest: a target quantity is already the most we
    want to hold for this weight, so rounding up would size past what
    the strategy actually asked for.
    """
    if step_size <= 0:
        return quantity
    steps = (quantity / step_size).to_integral_value(rounding=ROUND_DOWN)
    return steps * step_size


def target_quantity(
    target_weight: float,
    sleeve_equity: Decimal,
    price: Decimal,
    step_size: Decimal,
) -> Decimal:
    """Target weight -> a tradeable quantity, rounded down to the
    instrument's step size.

    Long-only (design rule 1): a non-positive weight or price is
    treated as flat (0), never a negative/short quantity.
    """
    if target_weight <= 0 or price <= 0:
        return Decimal(0)
    # str(float), not Decimal(float): captures the "nice" decimal
    # repr (0.15) rather than float's exact noisy binary expansion
    # (0.1499999999999999944...).
    raw_quantity = Decimal(str(target_weight)) * sleeve_equity / price
    return round_to_step(raw_quantity, step_size)


def implied_weight(quantity: Decimal, price: Decimal, sleeve_equity: Decimal) -> float:
    """The weight actually implied by a held quantity — what
    ``portfolio.sizing.should_trade`` compares a new target against."""
    if sleeve_equity <= 0:
        return 0.0
    return float(quantity * price / sleeve_equity)
