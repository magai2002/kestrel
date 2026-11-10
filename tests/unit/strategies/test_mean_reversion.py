from __future__ import annotations

import math

import pandas as pd
import pytest

from strategies.base import StrategyContext
from strategies.mean_reversion import (
    MeanReversionParams,
    ShortTermMeanReversion,
    compute_weights,
    rsi,
)


def test_rsi_period_1_hand_computed() -> None:
    # period=1 makes Wilder's smoothing trivial (alpha=1, no memory):
    # RSI is exactly 100 after a pure gain, exactly 0 after a pure loss.
    prices = pd.Series([100.0, 105.0, 95.0, 97.0, 90.0])
    values = rsi(prices, period=1)
    assert math.isnan(values.iloc[0])
    assert values.iloc[1] == pytest.approx(100.0)  # +5
    assert values.iloc[2] == pytest.approx(0.0)  # -10
    assert values.iloc[3] == pytest.approx(100.0)  # +2
    assert values.iloc[4] == pytest.approx(0.0)  # -7


def test_rsi_all_gains_is_100() -> None:
    prices = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
    values = rsi(prices, period=2)
    assert values.iloc[-1] == pytest.approx(100.0)


def _dip_and_recover_prices() -> pd.Series:
    # Flat, then a sharp 3-bar drop (should push RSI(2) well below 10),
    # then a sharp recovery (should push RSI(2) back above 50), then flat.
    flat_pre = [100.0] * 10
    drop = [95.0, 88.0, 80.0]
    recover = [95.0, 110.0, 115.0]
    flat_post = [115.0] * 5
    return pd.Series(flat_pre + drop + recover + flat_post)


def test_enters_after_sharp_drop_and_exits_after_recovery() -> None:
    prices = pd.DataFrame({"SPY": _dip_and_recover_prices()})
    params = MeanReversionParams(symbol="SPY", rsi_period=2)
    weights = compute_weights(prices, params)["SPY"]

    assert weights.iloc[:10].sum() == 0  # flat period before the drop: never entered
    assert weights.iloc[12] == 1.0  # deep in the drop: should be in a trade
    assert weights.iloc[-1] == 0.0  # well after recovery: exited


def test_weight_is_binary() -> None:
    prices = pd.DataFrame({"SPY": _dip_and_recover_prices()})
    params = MeanReversionParams(symbol="SPY", rsi_period=2, position_weight=1.0)
    weights = compute_weights(prices, params)["SPY"]
    assert set(weights.unique()) <= {0.0, 1.0}


def test_other_columns_stay_zero() -> None:
    prices = pd.DataFrame({"SPY": _dip_and_recover_prices(), "QQQ": _dip_and_recover_prices()})
    params = MeanReversionParams(symbol="SPY", rsi_period=2)
    weights = compute_weights(prices, params)
    assert (weights["QQQ"] == 0.0).all()


def test_raises_if_symbol_missing() -> None:
    prices = pd.DataFrame({"QQQ": [1.0, 2.0, 3.0]})
    with pytest.raises(ValueError, match="SPY"):
        compute_weights(prices, MeanReversionParams(symbol="SPY"))


def test_on_bar_replays_the_same_entry_and_exit() -> None:
    prices = _dip_and_recover_prices()
    expected = compute_weights(
        pd.DataFrame({"SPY": prices}), MeanReversionParams(symbol="SPY", rsi_period=2)
    )["SPY"]

    strategy = ShortTermMeanReversion(MeanReversionParams(symbol="SPY", rsi_period=2))
    results = []
    for i in range(len(prices)):
        history = {"SPY": pd.DataFrame({"close": prices.iloc[: i + 1]})}
        ctx = StrategyContext(history=history, now=pd.Timestamp("2024-01-01", tz="UTC"))
        results.append(strategy.on_bar(ctx)["SPY"])

    assert results == list(expected)
