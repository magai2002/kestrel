from __future__ import annotations

import pandas as pd
import pytest

from strategies.base import StrategyContext
from strategies.baselines import (
    BuyAndHold,
    BuyAndHoldParams,
    EqualWeightRebalanced,
    EqualWeightRebalancedParams,
    buy_and_hold_weights,
    equal_weight_rebalanced_weights,
)


def test_equal_weight_rebalanced_is_constant() -> None:
    prices = pd.DataFrame({"a": [100.0, 110.0, 90.0], "b": [50.0, 45.0, 60.0]})
    weights = equal_weight_rebalanced_weights(prices, EqualWeightRebalancedParams())
    assert (weights["a"] == 0.5).all()
    assert (weights["b"] == 0.5).all()


def test_equal_weight_respects_gross_exposure() -> None:
    prices = pd.DataFrame({"a": [100.0], "b": [50.0]})
    weights = equal_weight_rebalanced_weights(
        prices, EqualWeightRebalancedParams(target_gross_exposure=0.5)
    )
    assert weights.iloc[0].sum() == pytest.approx(0.5)


def test_buy_and_hold_drifts_with_relative_prices() -> None:
    # Equal-weight entry (0.5/0.5); 'a' doubles, 'b' is flat -> 'a' should
    # end up holding roughly 2/3 of total value, not stay at 50%.
    prices = pd.DataFrame({"a": [100.0, 200.0], "b": [100.0, 100.0]})
    weights = buy_and_hold_weights(prices, BuyAndHoldParams())
    assert weights["a"].iloc[0] == pytest.approx(0.5)
    assert weights["a"].iloc[1] == pytest.approx(2 / 3)
    assert weights["b"].iloc[1] == pytest.approx(1 / 3)


def test_buy_and_hold_honors_custom_initial_weights() -> None:
    prices = pd.DataFrame({"a": [100.0, 100.0], "b": [100.0, 100.0]})
    weights = buy_and_hold_weights(prices, BuyAndHoldParams(initial_weights={"a": 0.8, "b": 0.2}))
    assert weights["a"].iloc[0] == pytest.approx(0.8)
    assert weights["b"].iloc[0] == pytest.approx(0.2)


def test_buy_and_hold_implies_a_constant_held_quantity() -> None:
    """The defining property of buy-and-hold: the quantity implied by
    the weight (quantity = weight * total_value / price) never changes,
    even though the weight itself drifts. This is the property that
    makes it legitimately zero-turnover in the *real* system, where the
    portfolio layer converts weight -> quantity and the order manager
    only trades a quantity difference (design rule 1) — see this
    module's and research/vectorized.py's docstrings for why the
    vectorized runner's weight-diff cost proxy can't see that and
    (correctly, given its own simplifying assumptions) still prices in
    some drift-driven "turnover" for a passively-drifting weight."""
    prices = pd.DataFrame({"a": [100.0, 150.0, 90.0, 200.0], "b": [50.0, 40.0, 70.0, 55.0]})
    weights = buy_and_hold_weights(prices, BuyAndHoldParams())

    implied_qty_a = 0.5 / prices["a"].iloc[0]
    implied_qty_b = 0.5 / prices["b"].iloc[0]
    value_a = implied_qty_a * prices["a"]
    value_b = implied_qty_b * prices["b"]
    total_value = value_a + value_b

    pd.testing.assert_series_equal(weights["a"], value_a / total_value, check_names=False)
    pd.testing.assert_series_equal(weights["b"], value_b / total_value, check_names=False)


def test_equal_weight_strategy_on_bar() -> None:
    strategy = EqualWeightRebalanced()
    history = {
        "a": pd.DataFrame({"close": [100.0, 110.0]}),
        "b": pd.DataFrame({"close": [50.0, 45.0]}),
    }
    ctx = StrategyContext(history=history, now=pd.Timestamp("2024-01-02", tz="UTC"))
    weights = strategy.on_bar(ctx)
    assert weights == {"a": 0.5, "b": 0.5}


def test_buy_and_hold_strategy_on_bar_matches_vectorized_replay() -> None:
    prices = pd.DataFrame({"a": [100.0, 150.0, 90.0, 200.0], "b": [50.0, 40.0, 70.0, 55.0]})
    expected = buy_and_hold_weights(prices, BuyAndHoldParams())

    strategy = BuyAndHold()
    for i in range(len(prices)):
        history = {
            col: pd.DataFrame({"close": prices[col].iloc[: i + 1]}) for col in prices.columns
        }
        ctx = StrategyContext(history=history, now=pd.Timestamp("2024-01-01", tz="UTC"))
        result = strategy.on_bar(ctx)
        for col in prices.columns:
            assert result[col] == pytest.approx(expected[col].iloc[i])


def test_buy_and_hold_weights_sum_to_one() -> None:
    strategy = BuyAndHold()
    history = {
        "a": pd.DataFrame({"close": [100.0, 120.0]}),
        "b": pd.DataFrame({"close": [200.0, 180.0]}),
    }
    ctx = StrategyContext(history=history, now=pd.Timestamp("2024-01-01", tz="UTC"))
    weights = strategy.on_bar(ctx)
    assert sum(weights.values()) == pytest.approx(1.0)
