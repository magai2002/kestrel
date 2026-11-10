from decimal import Decimal

from portfolio.construction import implied_weight, round_to_step, target_quantity


def test_round_to_step_hand_computed() -> None:
    assert round_to_step(Decimal("0.123456"), Decimal("0.00001")) == Decimal("0.12345")


def test_round_to_step_rounds_down_not_nearest() -> None:
    # 0.19999 is one step shy of 0.2 at step size 0.1 -- must round
    # DOWN to 0.1, not up to 0.2.
    assert round_to_step(Decimal("0.19999"), Decimal("0.1")) == Decimal("0.1")


def test_round_to_step_zero_step_is_a_no_op() -> None:
    assert round_to_step(Decimal("1.23456"), Decimal("0")) == Decimal("1.23456")


def test_target_quantity_hand_computed() -> None:
    # 50% of $10,000 sleeve at $50,000/BTC = 0.1 BTC exactly.
    qty = target_quantity(
        target_weight=0.5,
        sleeve_equity=Decimal("10000"),
        price=Decimal("50000"),
        step_size=Decimal("0.00001"),
    )
    assert qty == Decimal("0.1")


def test_target_quantity_rounds_down_to_step() -> None:
    qty = target_quantity(
        target_weight=0.33,
        sleeve_equity=Decimal("10000"),
        price=Decimal("50000"),
        step_size=Decimal("0.001"),
    )
    # raw = 0.33 * 10000 / 50000 = 0.066 -- already a multiple of 0.001.
    assert qty == Decimal("0.066")


def test_target_quantity_zero_weight_is_flat() -> None:
    qty = target_quantity(0.0, Decimal("10000"), Decimal("50000"), Decimal("0.00001"))
    assert qty == Decimal("0")


def test_target_quantity_negative_weight_is_flat_not_short() -> None:
    qty = target_quantity(-0.5, Decimal("10000"), Decimal("50000"), Decimal("0.00001"))
    assert qty == Decimal("0")


def test_implied_weight_hand_computed() -> None:
    # 0.1 BTC @ $50,000 = $5,000 of a $10,000 sleeve = 50%.
    weight = implied_weight(Decimal("0.1"), Decimal("50000"), Decimal("10000"))
    assert weight == 0.5


def test_implied_weight_zero_equity_is_zero() -> None:
    assert implied_weight(Decimal("1"), Decimal("50000"), Decimal("0")) == 0.0
