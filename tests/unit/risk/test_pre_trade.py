from datetime import UTC, datetime
from decimal import Decimal

from core.order import Order, OrderSide
from risk.limits import GroupCap, RiskLimits
from risk.pre_trade import (
    check_gross_exposure,
    check_group_cap,
    check_lot_and_notional,
    check_max_order_notional,
    check_price_band,
    check_weight_cap,
    evaluate_order,
)


def _order(quantity: str = "0.1", instrument_id: str = "binance:BTCUSDT") -> Order:
    return Order(
        client_order_id="x",
        instrument_id=instrument_id,
        side=OrderSide.BUY,
        quantity=Decimal(quantity),
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )


def test_weight_cap_approves_within_limit() -> None:
    assert check_weight_cap(0.5, RiskLimits(per_instrument_weight_cap=0.6)).approved


def test_weight_cap_rejects_over_limit() -> None:
    decision = check_weight_cap(0.7, RiskLimits(per_instrument_weight_cap=0.6))
    assert not decision.approved
    assert "cap" in decision.reason


def test_group_cap_rejects_when_group_total_exceeds_cap() -> None:
    limits = RiskLimits(
        group_caps=(GroupCap(name="equity_indices", instrument_ids=("a", "b", "c"), cap=0.5),)
    )
    decision = check_group_cap("a", {"a": 0.3, "b": 0.3, "c": 0.0}, limits)
    assert not decision.approved


def test_group_cap_approves_when_instrument_not_in_any_group() -> None:
    limits = RiskLimits(group_caps=(GroupCap(name="g", instrument_ids=("a",), cap=0.5),))
    assert check_group_cap("z", {"z": 0.9}, limits).approved


def test_gross_exposure_rejects_over_100_percent() -> None:
    decision = check_gross_exposure({"a": 0.6, "b": 0.6}, RiskLimits(gross_exposure_cap=1.0))
    assert not decision.approved


def test_max_order_notional_fat_finger_guard() -> None:
    limits = RiskLimits(max_order_notional_pct=0.30)
    decision = check_max_order_notional(Decimal("4000"), Decimal("10000"), limits)
    assert not decision.approved  # 40% > 30% cap


def test_price_band_approves_within_band() -> None:
    limits = RiskLimits(price_band_pct=0.02)
    decision = check_price_band(Decimal("101"), Decimal("100"), limits)
    assert decision.approved


def test_price_band_rejects_outside_band() -> None:
    limits = RiskLimits(price_band_pct=0.02)
    decision = check_price_band(Decimal("110"), Decimal("100"), limits)
    assert not decision.approved


def test_price_band_approves_when_no_reference_price_yet() -> None:
    decision = check_price_band(Decimal("100"), Decimal("0"), RiskLimits())
    assert decision.approved


def test_lot_and_notional_rejects_non_step_multiple() -> None:
    decision = check_lot_and_notional(
        Decimal("0.123"), Decimal("50000"), Decimal("0.01"), Decimal("0")
    )
    assert not decision.approved


def test_lot_and_notional_rejects_below_min_notional() -> None:
    # 0.00009 * 50000 = 4.5, below a $5 minimum.
    decision = check_lot_and_notional(
        Decimal("0.00009"), Decimal("50000"), Decimal("0.00001"), Decimal("5")
    )
    assert not decision.approved


def test_lot_and_notional_exactly_at_min_notional_is_approved() -> None:
    # 0.0001 * 50000 = 5.0 exactly -- boundary should pass (>=), not fail.
    decision = check_lot_and_notional(
        Decimal("0.0001"), Decimal("50000"), Decimal("0.00001"), Decimal("5")
    )
    assert decision.approved


def test_evaluate_order_approves_clean_order() -> None:
    decision = evaluate_order(
        order=_order("0.05"),  # notional 2500 = 25% of sleeve equity, under every default cap
        reference_price=Decimal("50000"),
        step_size=Decimal("0.00001"),
        min_notional=Decimal("5"),
        proposed_weights={"binance:BTCUSDT": 0.5},
        sleeve_equity=Decimal("10000"),
        last_trade_price=Decimal("50000"),
        limits=RiskLimits(per_instrument_weight_cap=0.60),  # crypto-sleeve-style default
    )
    assert decision.approved


def test_evaluate_order_reports_first_breached_check() -> None:
    # Weight cap breached AND order notional too big -- weight cap check
    # runs first, so that's the reason reported.
    decision = evaluate_order(
        order=_order("1.0"),
        reference_price=Decimal("50000"),
        step_size=Decimal("0.00001"),
        min_notional=Decimal("5"),
        proposed_weights={"binance:BTCUSDT": 0.95},
        sleeve_equity=Decimal("10000"),
        last_trade_price=Decimal("50000"),
        limits=RiskLimits(per_instrument_weight_cap=0.60, max_order_notional_pct=0.30),
    )
    assert not decision.approved
    assert "cap" in decision.reason
