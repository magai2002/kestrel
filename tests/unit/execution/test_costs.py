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
