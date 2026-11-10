from __future__ import annotations

import pandas as pd
import pytest

from research import vectorized


def test_apply_buffer_holds_through_small_changes() -> None:
    weights = pd.DataFrame({"a": [0.50, 0.52, 0.55, 0.58]})
    buffered = vectorized.apply_buffer(weights, buffer_pct=0.10)
    # Every step is within 10% of the first held weight (0.50), so it
    # never trades: held stays 0.50 throughout.
    assert (buffered["a"] == 0.50).all()


def test_apply_buffer_trades_once_threshold_crossed() -> None:
    weights = pd.DataFrame({"a": [0.50, 0.55, 0.65, 0.65]})
    buffered = vectorized.apply_buffer(weights, buffer_pct=0.10)
    # 0.50 -> 0.55 (5% move, held) -> 0.65 (15% move from 0.50, trades) -> 0.65 (no move).
    assert list(buffered["a"]) == [0.50, 0.50, 0.65, 0.65]


def test_apply_buffer_zero_percent_is_a_no_op() -> None:
    weights = pd.DataFrame({"a": [0.1, 0.2, 0.15]})
    buffered = vectorized.apply_buffer(weights, buffer_pct=0.0)
    pd.testing.assert_frame_equal(buffered, weights)


def test_buffering_reduces_turnover() -> None:
    # A noisy target series around a stable level -- buffering should
    # cut turnover sharply without changing the broad positioning.
    weights = pd.DataFrame({"a": [0.5, 0.52, 0.49, 0.53, 0.48, 0.51]})
    unbuffered_turnover = weights.diff().abs().sum(axis=1).iloc[1:].mean()
    buffered = vectorized.apply_buffer(weights, buffer_pct=0.10)
    buffered_turnover = buffered.diff().abs().sum(axis=1).iloc[1:].mean()
    assert buffered_turnover < unbuffered_turnover
    assert buffered_turnover == pytest.approx(0.0)


def _prices(values: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"a": values}, index=pd.RangeIndex(len(values)))


def test_no_look_ahead_decision_applies_to_the_next_bar_only() -> None:
    prices = _prices([100.0, 110.0, 121.0, 133.1])  # +10% each bar
    # Target weight 1.0 decided only at bar 0 (index 0); flat elsewhere.
    weights = pd.DataFrame({"a": [1.0, 0.0, 0.0, 0.0]})
    result = vectorized.run(prices, weights)
    # bar0's decision earns bar1's return (100->110, +10%), not bar0's own.
    assert result.gross_returns.iloc[0] == pytest.approx(0.0)
    assert result.gross_returns.iloc[1] == pytest.approx(0.10)
    assert result.gross_returns.iloc[2] == pytest.approx(0.0)


def test_changing_a_future_weight_does_not_change_past_returns() -> None:
    prices = _prices([100.0, 110.0, 121.0, 133.1, 146.41])
    weights_a = pd.DataFrame({"a": [1.0, 1.0, 1.0, 0.0, 0.0]})
    weights_b = pd.DataFrame({"a": [1.0, 1.0, 1.0, 1.0, 1.0]})  # differs only after index 2
    result_a = vectorized.run(prices, weights_a)
    result_b = vectorized.run(prices, weights_b)
    pd.testing.assert_series_equal(result_a.gross_returns.iloc[:3], result_b.gross_returns.iloc[:3])


def test_cost_is_charged_on_the_bar_the_new_position_starts_earning() -> None:
    prices = _prices([100.0, 100.0, 110.0, 110.0])
    # Flat, then target jumps to 1.0 at bar1 -- the resulting trade
    # takes effect (and should be charged) on bar2, where the new
    # position actually earns a return (100 -> 110, +10%).
    weights = pd.DataFrame({"a": [0.0, 1.0, 1.0, 1.0]})
    result = vectorized.run(prices, weights, cost_bps=100.0)  # 1% per unit turnover
    assert result.turnover_per_bar.iloc[1] == pytest.approx(0.0)  # nothing held yet on bar1
    assert result.turnover_per_bar.iloc[2] == pytest.approx(1.0)  # 0 -> 1.0 trade lands here
    assert result.net_returns.iloc[1] == pytest.approx(0.0)
    # bar2: +10% gross, minus 1% of 1.0 turnover = +9%.
    assert result.net_returns.iloc[2] == pytest.approx(0.10 - 0.01)


def test_equity_curve_compounds_net_returns() -> None:
    prices = _prices([100.0, 110.0, 121.0])
    weights = pd.DataFrame({"a": [1.0, 1.0, 1.0]})
    result = vectorized.run(prices, weights)
    assert result.equity_curve.iloc[-1] == pytest.approx((1.10) * (1.10))


def test_multi_instrument_weights_sum_contributions() -> None:
    prices = pd.DataFrame({"a": [100.0, 110.0], "b": [50.0, 45.0]})
    weights = pd.DataFrame({"a": [0.5, 0.5], "b": [0.5, 0.5]})
    result = vectorized.run(prices, weights)
    # bar1: 0.5*+10% + 0.5*-10% = 0%
    assert result.gross_returns.iloc[1] == pytest.approx(0.0)
