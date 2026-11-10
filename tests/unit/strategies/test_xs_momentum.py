from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategies.base import StrategyContext
from strategies.xs_momentum import (
    CrossSectionalMomentum,
    XSMomentumParams,
    _lookback_return,
    _rebalance_positions,
    compute_weights,
)


def test_rebalance_positions_hand_computed() -> None:
    index = pd.date_range("2024-01-01", "2024-02-29", freq="D")
    positions = _rebalance_positions(index, "ME")
    # Jan has 31 days (positions 0..30), so Jan's last position is 30;
    # Feb 2024 is a leap year (29 days), starting at position 31, so
    # Feb's last position is 31 + 29 - 1 = 59.
    assert positions == [30, 59]


def test_rebalance_positions_includes_trailing_partial_period() -> None:
    # pandas' resample().last() includes a final partial bin if it has
    # data in it, even though the calendar period hasn't ended --
    # that's the "most recent decision" we want, not a bug.
    index = pd.date_range("2024-01-01", "2024-03-05", freq="D")
    positions = _rebalance_positions(index, "ME")
    assert positions == [30, 59, 64]


def test_lookback_return_hand_computed() -> None:
    index = pd.date_range("2024-01-01", periods=100, freq="D")
    prices = pd.Series(np.linspace(100.0, 199.0, 100), index=index)
    # 1 month back from day index 60 (2024-03-01) is 2024-02-01, day index 31.
    ret = _lookback_return(prices, at_position=60, lookback_months=1, index=index)
    expected = prices.iloc[60] / prices.iloc[31] - 1.0
    assert ret == pytest.approx(expected)


def test_lookback_return_nan_when_not_enough_history() -> None:
    index = pd.date_range("2024-01-01", periods=10, freq="D")
    prices = pd.Series(np.arange(10, dtype=float) + 100.0, index=index)
    assert np.isnan(_lookback_return(prices, at_position=5, lookback_months=12, index=index))


def _make_series(
    index: pd.DatetimeIndex, start: float, monthly_drift: float, daily_vol: float, seed: int
) -> pd.Series:
    rng = np.random.default_rng(seed)
    daily_drift = monthly_drift / 30.0
    log_returns = daily_drift + rng.normal(0, daily_vol, len(index))
    return pd.Series(start * np.exp(np.cumsum(log_returns)), index=index)


def _three_instrument_frame(n_days: int = 80) -> pd.DataFrame:
    # Low daily vol (0.2%, ~1.1% monthly) relative to the drifts below
    # keeps each series' sign reliably determined by its drift rather
    # than by noise at whichever bar a test happens to sample.
    index = pd.date_range("2024-01-01", periods=n_days, freq="D")
    return pd.DataFrame(
        {
            "winner": _make_series(index, 100.0, monthly_drift=0.25, daily_vol=0.002, seed=1),
            "loser": _make_series(index, 100.0, monthly_drift=-0.20, daily_vol=0.002, seed=2),
            "flat": _make_series(index, 100.0, monthly_drift=0.05, daily_vol=0.002, seed=3),
        }
    )


def test_dual_momentum_excludes_negative_momentum_even_if_top_ranked() -> None:
    prices = _three_instrument_frame()
    params = XSMomentumParams(lookback_months=1, top_n=3, vol_span_days=10, bars_per_day=1.0)
    weights = compute_weights(prices, params)
    last_row = weights.iloc[-1]
    assert last_row["loser"] == 0.0  # negative trailing return -> cash, despite being in top_n
    assert last_row["winner"] > 0.0
    assert last_row["flat"] > 0.0


def test_fewer_survivors_than_top_n_scales_down_total_exposure() -> None:
    prices = _three_instrument_frame()
    # top_n=3 but only 2 instruments have positive momentum -> total
    # weight should be ~2/3, not fully 1.0.
    params = XSMomentumParams(lookback_months=1, top_n=3, vol_span_days=10, bars_per_day=1.0)
    weights = compute_weights(prices, params)
    last_row = weights.iloc[-1]
    assert last_row.sum() == pytest.approx(2 / 3, abs=0.02)


def test_all_cash_when_nothing_has_positive_momentum() -> None:
    index = pd.date_range("2024-01-01", periods=80, freq="D")
    prices = pd.DataFrame(
        {
            "a": _make_series(index, 100.0, monthly_drift=-0.1, daily_vol=0.01, seed=1),
            "b": _make_series(index, 100.0, monthly_drift=-0.2, daily_vol=0.01, seed=2),
        }
    )
    params = XSMomentumParams(lookback_months=1, top_n=2, vol_span_days=10, bars_per_day=1.0)
    weights = compute_weights(prices, params)
    assert weights.iloc[-1].sum() == pytest.approx(0.0, abs=1e-9)


def test_weights_only_change_at_rebalance_bars() -> None:
    prices = _three_instrument_frame()
    params = XSMomentumParams(lookback_months=1, top_n=3, vol_span_days=10, bars_per_day=1.0)
    weights = compute_weights(prices, params)
    rebalance_positions = set(_rebalance_positions(prices.index, params.rebalance_freq))
    changed = weights.diff().abs().sum(axis=1)
    changed_positions = {i for i, v in enumerate(changed) if v > 1e-12}
    assert changed_positions <= rebalance_positions


def test_on_bar_matches_vectorized_replay() -> None:
    prices = _three_instrument_frame()
    params = XSMomentumParams(lookback_months=1, top_n=3, vol_span_days=10, bars_per_day=1.0)
    expected = compute_weights(prices, params)

    strategy = CrossSectionalMomentum(params=params)
    history = {col: pd.DataFrame({"close": prices[col]}) for col in prices.columns}
    ctx = StrategyContext(history=history, now=pd.Timestamp("2024-01-01", tz="UTC"))
    result = strategy.on_bar(ctx)
    # Not bit-identical: the windowed replay's EWMA vol estimate warms
    # up from a different starting point than the full-history version,
    # so it converges close but never exactly matches -- this is the
    # same "stated tolerance" the Phase 2 parity tests use, just at
    # strategy-unit-test scale.
    for col in prices.columns:
        assert result[col] == pytest.approx(expected[col].iloc[-1], abs=1e-4)
