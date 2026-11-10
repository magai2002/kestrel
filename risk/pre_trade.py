"""Pre-trade risk checks (spec Section 6.3): reject one order at a
time, with a reason, before it ever reaches a broker. Each check is
independent and composable; ``evaluate_order`` runs all of them and
stops at the first rejection — the reason reported is always the first
limit actually breached, not an arbitrary one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core.order import Order
from risk.limits import RiskLimits


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str | None = None


def check_weight_cap(proposed_weight: float, limits: RiskLimits) -> RiskDecision:
    if proposed_weight > limits.per_instrument_weight_cap:
        return RiskDecision(
            False,
            f"proposed weight {proposed_weight:.1%} exceeds per-instrument "
            f"cap {limits.per_instrument_weight_cap:.1%}",
        )
    return RiskDecision(True)


def check_group_cap(
    instrument_id: str, proposed_weights: dict[str, float], limits: RiskLimits
) -> RiskDecision:
    for group in limits.group_caps:
        if instrument_id not in group.instrument_ids:
            continue
        group_total = sum(proposed_weights.get(i, 0.0) for i in group.instrument_ids)
        if group_total > group.cap:
            return RiskDecision(
                False,
                f"group {group.name!r} total weight {group_total:.1%} exceeds cap {group.cap:.1%}",
            )
    return RiskDecision(True)


def check_gross_exposure(proposed_weights: dict[str, float], limits: RiskLimits) -> RiskDecision:
    gross = sum(abs(w) for w in proposed_weights.values())
    if gross > limits.gross_exposure_cap:
        return RiskDecision(
            False, f"gross exposure {gross:.1%} exceeds cap {limits.gross_exposure_cap:.1%}"
        )
    return RiskDecision(True)


def check_max_order_notional(
    order_notional: Decimal, sleeve_equity: Decimal, limits: RiskLimits
) -> RiskDecision:
    max_notional = sleeve_equity * Decimal(str(limits.max_order_notional_pct))
    if order_notional > max_notional:
        return RiskDecision(
            False, f"order notional {order_notional} exceeds fat-finger cap {max_notional}"
        )
    return RiskDecision(True)


def check_price_band(
    order_price: Decimal, last_trade_price: Decimal, limits: RiskLimits
) -> RiskDecision:
    if last_trade_price <= 0:
        return RiskDecision(True)  # no reference price yet -- nothing to check against
    deviation = abs(order_price - last_trade_price) / last_trade_price
    band = Decimal(str(limits.price_band_pct))
    if deviation > band:
        return RiskDecision(
            False,
            f"order price {order_price} is {deviation:.1%} from last trade "
            f"{last_trade_price}, outside the {limits.price_band_pct:.1%} band",
        )
    return RiskDecision(True)


def check_lot_and_notional(
    quantity: Decimal, price: Decimal, step_size: Decimal, min_notional: Decimal
) -> RiskDecision:
    if step_size > 0 and (quantity % step_size) != 0:
        return RiskDecision(
            False, f"quantity {quantity} is not a multiple of step size {step_size}"
        )
    notional = quantity * price
    if min_notional > 0 and notional < min_notional:
        return RiskDecision(False, f"notional {notional} is below minimum {min_notional}")
    return RiskDecision(True)


def evaluate_order(
    order: Order,
    reference_price: Decimal,
    step_size: Decimal,
    min_notional: Decimal,
    proposed_weights: dict[str, float],
    sleeve_equity: Decimal,
    last_trade_price: Decimal,
    limits: RiskLimits,
) -> RiskDecision:
    """Every check, in order, stopping at the first rejection.

    ``reference_price`` is the order's limit price if it has one,
    otherwise ``last_trade_price`` (a market order has no price of its
    own to check a band against except the price it would fill near).
    """
    checks = (
        check_weight_cap(proposed_weights.get(order.instrument_id, 0.0), limits),
        check_group_cap(order.instrument_id, proposed_weights, limits),
        check_gross_exposure(proposed_weights, limits),
        check_max_order_notional(order.quantity * reference_price, sleeve_equity, limits),
        check_price_band(reference_price, last_trade_price, limits),
        check_lot_and_notional(order.quantity, reference_price, step_size, min_notional),
    )
    for decision in checks:
        if not decision.approved:
            return decision
    return RiskDecision(True)
