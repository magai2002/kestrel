from decimal import Decimal

from execution.costs import (
    CostModel,
    belgian_tob,
    commission,
    half_spread_price_adjustment,
    slippage_price_adjustment,
)


def test_default_cost_model_uses_verified_kraken_taker_fee() -> None:
    # 0.80% -- confirmed against Kraken's live fee schedule, not the
    # spec's stale 0.40% (see module docstring).
    assert CostModel().fee_pct == Decimal("0.0080")


def test_half_spread_hand_computed() -> None:
    # 2 bps full spread -> 1 bp half-spread on a 50000 price = 5.0
    assert half_spread_price_adjustment(Decimal("50000"), Decimal("2")) == Decimal("5.0")


def test_slippage_hand_computed() -> None:
    # k=0.1, vol=0.02 -> 0.002 * 50000 = 100
    assert slippage_price_adjustment(Decimal("50000"), Decimal("0.02"), Decimal("0.1")) == Decimal(
        "100.0"
    )


def test_commission_hand_computed() -> None:
    assert commission(Decimal("10000"), Decimal("0.008")) == Decimal("80")


def test_belgian_tob_zero_when_not_applicable() -> None:
    assert belgian_tob(Decimal("10000"), Decimal("0"), None) == Decimal(0)


def test_belgian_tob_hand_computed() -> None:
    assert belgian_tob(Decimal("10000"), Decimal("0.0035"), None) == Decimal("35.0")


def test_belgian_tob_respects_cap() -> None:
    # 1,000,000 * 0.35% = 3500, capped at 1600.
    assert belgian_tob(Decimal("1000000"), Decimal("0.0035"), Decimal("1600")) == Decimal("1600")


def test_belgian_tob_under_cap_is_unaffected() -> None:
    assert belgian_tob(Decimal("10000"), Decimal("0.0035"), Decimal("1600")) == Decimal("35.0")


def test_commission_minimum_dominates_small_orders() -> None:
    """IBKR-style: 0.05% of $500 is $0.25, but the per-order minimum is
    $1.70 — at retail size the minimum is the real cost."""
    from decimal import Decimal

    from execution.costs import commission

    assert commission(Decimal("500"), Decimal("0.0005"), Decimal("1.70")) == Decimal("1.70")
    assert commission(Decimal("10000"), Decimal("0.0005"), Decimal("1.70")) == Decimal("5.0000")


def test_commission_adds_fixed_fee() -> None:
    from decimal import Decimal

    from execution.costs import commission

    assert commission(Decimal("1000"), Decimal("0.0005"), Decimal("0"), Decimal("0.21")) == Decimal(
        "0.7100"
    )


def test_order_cost_is_commission_plus_tob() -> None:
    from decimal import Decimal

    from execution.costs import CostModel, order_cost

    cm = CostModel(fee_pct=Decimal("0.0005"), min_fee=Decimal("1.70"), tob_pct=Decimal("0.0012"))
    assert order_cost(Decimal("1000"), cm) == Decimal("1.70") + Decimal("1.2000")
