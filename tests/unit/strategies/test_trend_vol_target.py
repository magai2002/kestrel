from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from strategies.base import StrategyContext
from strategies.trend_vol_target import (
    TrendVolTarget,
    TrendVolTargetParams,
    compute_weights,
    required_lookback_bars,
)

# Default params need 256-day slow speed * 5 = 1280 bars to warm up;
# give every synthetic series comfortable headroom beyond that.
N_BARS = 1800


def _trending_prices(daily_drift: float, daily_noise_std: float, seed: int = 0) -> pd.Series:
    rng = np.random.default_rng(seed)
    log_returns = daily_drift + rng.normal(0, daily_noise_std, N_BARS)
    return pd.Series(100.0 * np.exp(np.cumsum(log_returns)))


def _choppy_prices(daily_noise_std: float, seed: int = 1) -> pd.Series:
    return _trending_prices(daily_drift=0.0, daily_noise_std=daily_noise_std, seed=seed)


def test_weights_always_in_unit_interval() -> None:
    prices = pd.DataFrame({"a": _trending_prices(0.002, 0.01)})
    weights = compute_weights(prices, TrendVolTargetParams())
    assert (weights["a"] >= 0.0).all()
    assert (weights["a"] <= 1.0).all()


def test_sustained_uptrend_produces_sustained_positive_weight() -> None:
    prices = pd.DataFrame({"a": _trending_prices(daily_drift=0.003, daily_noise_std=0.005)})
    weights = compute_weights(prices, TrendVolTargetParams())
    tail = weights["a"].iloc[-200:]
    assert (tail > 0.1).mean() > 0.8  # mostly meaningfully long, most of the time


def test_sustained_downtrend_stays_flat_not_short() -> None:
    prices = pd.DataFrame({"a": _trending_prices(daily_drift=-0.003, daily_noise_std=0.005)})
    weights = compute_weights(prices, TrendVolTargetParams())
    tail = weights["a"].iloc[-200:]
    assert (tail >= 0.0).all()  # never negative/short
    assert tail.mean() < 0.05  # effectively flat


def test_choppy_sideways_market_stays_near_flat() -> None:
    prices = pd.DataFrame({"a": _choppy_prices(daily_noise_std=0.01)})
    weights = compute_weights(prices, TrendVolTargetParams())
    tail = weights["a"].iloc[-200:]
    # No persistent trend to follow -> average position should be small,
    # even if it occasionally pokes above zero on noise.
    assert tail.mean() < 0.15


def test_higher_volatility_instrument_gets_smaller_position_for_same_trend() -> None:
    low_vol = _trending_prices(daily_drift=0.003, daily_noise_std=0.003, seed=5)
    high_vol = _trending_prices(daily_drift=0.003, daily_noise_std=0.02, seed=5)
    prices = pd.DataFrame({"low": low_vol, "high": high_vol})
    weights = compute_weights(prices, TrendVolTargetParams())
    assert weights["low"].iloc[-200:].mean() > weights["high"].iloc[-200:].mean()


def test_rejects_fast_span_not_less_than_slow_span() -> None:
    with pytest.raises(ValidationError):
        TrendVolTargetParams(speeds_days=((64, 64),))


def test_rejects_empty_speeds() -> None:
    with pytest.raises(ValidationError):
        TrendVolTargetParams(speeds_days=())


def test_bars_per_day_scales_lookback() -> None:
    daily_params = TrendVolTargetParams(bars_per_day=1.0)
    crypto_4h_params = TrendVolTargetParams(bars_per_day=6.0)
    assert required_lookback_bars(crypto_4h_params) == required_lookback_bars(daily_params) * 6


def test_on_bar_matches_vectorized_replay_once_lookback_is_satisfied() -> None:
    prices = pd.DataFrame({"a": _trending_prices(0.003, 0.005)})
    params = TrendVolTargetParams()
    expected = compute_weights(prices, params)

    strategy = TrendVolTarget(params=params)
    history = {"a": pd.DataFrame({"close": prices["a"]})}
    # Check the last bar, where the windowed replay has its full lookback available.
    ctx = StrategyContext(history=history, now=pd.Timestamp("2024-01-01", tz="UTC"))
    result = strategy.on_bar(ctx)
    assert result["a"] == pytest.approx(expected["a"].iloc[-1], abs=1e-6)
