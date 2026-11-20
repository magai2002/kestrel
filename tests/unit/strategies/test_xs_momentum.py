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
    index = pd.date_range("2024-01-01", "2024-03-05", freq="D")
    positions = _rebalance_positions(index, "ME")
    # Jan has 31 days (positions 0..30), so Feb starts at 31; Feb 2024 is
    # a leap year (29 days), so Mar starts at 31 + 29 = 60. Position 0
    # is never a rebalance (nothing before it to know a period began).
    assert positions == [31, 60]


def test_newest_bar_of_an_open_month_is_not_a_rebalance() -> None:
    # The bug this timing fixes: with "last bar of the month", the newest
    # bar of a still-open month looked like a month-end, so every
    # trailing-window replay (on_bar, live) rebalanced on every bar.
    index = pd.date_range("2024-01-01", "2024-03-05", freq="D")
    for end in range(61, len(index)):
        assert _rebalance_positions(index[: end + 1], "ME") == [31, 60]


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


def test_on_bar_matches_full_history_on_every_bar() -> None:
    """The live/event path must make the same decision as the vectorized
    one (what validation scored) on *every* bar, not only month-ends.
    Real default params; 1e-6 absorbs only the EWMA warm-up difference."""
    rng = np.random.default_rng(0)
    index = pd.bdate_range("2020-01-01", "2023-06-30", tz="UTC")
    cols = list("ABCDEF")
    prices = pd.DataFrame(
        100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, (len(index), len(cols))), axis=0)),
        index=index,
        columns=cols,
    )
    params = XSMomentumParams()
    full = compute_weights(prices, params)
    strategy = CrossSectionalMomentum(params=params)
    for i in range(len(index) - 90, len(index)):
        history = {c: pd.DataFrame({"close": prices[c].iloc[: i + 1]}) for c in cols}
        got = strategy.on_bar(StrategyContext(history=history, now=index[i].to_pydatetime()))
        for c in cols:
            assert got[c] == pytest.approx(full[c].iloc[i], abs=1e-6), (index[i], c)


def test_explain_reports_the_weights_on_bar_returns() -> None:
    prices = _three_instrument_frame()
    params = XSMomentumParams(lookback_months=1, top_n=3, vol_span_days=10, bars_per_day=1.0)
    strategy = CrossSectionalMomentum(params=params)
    history = {col: pd.DataFrame({"close": prices[col]}) for col in prices.columns}
    ctx = StrategyContext(history=history, now=pd.Timestamp("2024-03-20", tz="UTC"))
    ex = strategy.explain(ctx)
    assert ex["weights"] == strategy.on_bar(ctx)
    assets = ex["assets"]
    assert isinstance(assets, dict)
    assert assets["loser"]["positive"] is False and assets["loser"]["weight"] == 0.0
    assert assets["winner"]["rank"] == 1 and assets["winner"]["top_n"] is True
    assert ex["rebalanced_at"] == "2024-03-01T00:00:00"
