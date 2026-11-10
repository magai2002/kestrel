"""Spec Section 11, Property tests: "Risk limits never exceeded under
random targets."

Fuzzes each pre-trade check with random weights/notionals/prices and
confirms the invariant each one exists to enforce: if a check reports
``approved``, the actual breach condition it's guarding against is
genuinely false — not approved-by-accident on some edge case (zero,
negative, boundary-equal, empty dict) the hand-picked unit tests didn't
happen to try.
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from risk.limits import GroupCap, RiskLimits
from risk.pre_trade import (
    check_gross_exposure,
    check_group_cap,
    check_max_order_notional,
    check_price_band,
    check_weight_cap,
)

decimals = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("1000000"), places=2)
weights = st.floats(min_value=-2.0, max_value=2.0, allow_nan=False, allow_infinity=False)
caps = st.floats(min_value=0.0, max_value=2.0, allow_nan=False, allow_infinity=False)


@given(proposed_weight=weights, cap=caps)
@settings(max_examples=300)
def test_weight_cap_approval_implies_within_cap(proposed_weight: float, cap: float) -> None:
    decision = check_weight_cap(proposed_weight, RiskLimits(per_instrument_weight_cap=cap))
    if decision.approved:
        assert proposed_weight <= cap


@given(
    weights_dict=st.dictionaries(st.sampled_from(["a", "b", "c"]), weights, min_size=0, max_size=3),
    cap=caps,
)
@settings(max_examples=300)
def test_gross_exposure_approval_implies_within_cap(
    weights_dict: dict[str, float], cap: float
) -> None:
    decision = check_gross_exposure(weights_dict, RiskLimits(gross_exposure_cap=cap))
    if decision.approved:
        assert sum(abs(w) for w in weights_dict.values()) <= cap


@given(
    weights_dict=st.dictionaries(st.sampled_from(["a", "b", "c"]), weights, min_size=0, max_size=3),
    cap=caps,
)
@settings(max_examples=300)
def test_group_cap_approval_implies_group_total_within_cap(
    weights_dict: dict[str, float], cap: float
) -> None:
    limits = RiskLimits(group_caps=(GroupCap(name="g", instrument_ids=("a", "b", "c"), cap=cap),))
    decision = check_group_cap("a", weights_dict, limits)
    if decision.approved:
        group_total = sum(weights_dict.get(i, 0.0) for i in ("a", "b", "c"))
        assert group_total <= cap


@given(
    order_notional=decimals,
    sleeve_equity=decimals,
    pct=st.floats(min_value=0.0, max_value=2.0, allow_nan=False),
)
@settings(max_examples=300)
def test_max_order_notional_approval_implies_within_cap(
    order_notional: Decimal, sleeve_equity: Decimal, pct: float
) -> None:
    limits = RiskLimits(max_order_notional_pct=pct)
    decision = check_max_order_notional(order_notional, sleeve_equity, limits)
    if decision.approved:
        assert order_notional <= sleeve_equity * Decimal(str(pct))


@given(
    order_price=decimals,
    last_price=decimals,
    band=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
)
@settings(max_examples=300)
def test_price_band_approval_implies_within_band(
    order_price: Decimal, last_price: Decimal, band: float
) -> None:
    limits = RiskLimits(price_band_pct=band)
    decision = check_price_band(order_price, last_price, limits)
    if decision.approved and last_price > 0:
        deviation = abs(order_price - last_price) / last_price
        assert deviation <= Decimal(str(band))
