"""Spec Section 11, Property tests: "Order manager converges to
targets under random partial fills."

The order manager re-derives the order from (target - held) fresh
every bar (design rule 1), so a partial fill of ``fraction`` of the
current gap must move ``held`` exactly ``fraction`` of the way to
``target`` — monotonically, never overshooting, and exactly reaching
``target`` the moment a fill is complete (fraction == 1.0).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from core.order import OrderSide
from execution.order_manager import propose_order

NOW = datetime(2024, 1, 1, tzinfo=UTC)


@given(
    target=st.decimals(min_value=Decimal("-10"), max_value=Decimal("10"), places=4),
    held_start=st.decimals(min_value=Decimal("-10"), max_value=Decimal("10"), places=4),
    fractions=st.lists(
        st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=30,
    ),
)
@settings(max_examples=200)
def test_converges_without_overshoot_under_random_partial_fills(
    target: Decimal, held_start: Decimal, fractions: list[float]
) -> None:
    held = held_start
    for i, fraction in enumerate(fractions):
        distance_before = abs(target - held)
        order = propose_order("x", "X", target, held, "sleeve", "strat", NOW, seq=i)
        if order is None:
            assert held == target
            continue

        signed_gap = order.quantity if order.side == OrderSide.BUY else -order.quantity
        filled = Decimal(str(round(fraction, 9))) * signed_gap
        held = held + filled

        distance_after = abs(target - held)
        assert distance_after <= distance_before, "a partial fill must not move further from target"
        if fraction >= 1.0:
            assert held == target, "a full fill of the current gap must land exactly on target"
