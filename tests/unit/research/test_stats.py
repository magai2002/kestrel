"""Every metric checked against a value worked out by hand (spec
Phase 1 "done when": metrics are unit-tested against hand calculations).
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from research import stats


def test_total_return() -> None:
    curve = pd.Series([100.0, 121.0])
    assert stats.total_return(curve) == pytest.approx(0.21)


def test_cagr_two_periods_doubling_rate() -> None:
    # 1.0 -> 1.21 over 2 periods, 2 periods/year => exactly 1 year => CAGR = 21%.
    curve = pd.Series([1.0, 1.1, 1.21])
    assert stats.cagr(curve, periods_per_year=2) == pytest.approx(0.21)


def test_cagr_nan_on_non_positive_equity() -> None:
    curve = pd.Series([1.0, 0.5, -0.1])
    assert math.isnan(stats.cagr(curve, periods_per_year=252))


def test_cagr_zero_for_single_point() -> None:
    assert stats.cagr(pd.Series([1.0]), periods_per_year=252) == 0.0


def test_annualized_vol_hand_computed() -> None:
    # Alternating +/-2%: mean 0, sample std = sqrt(sum((x)^2)/(n-1))
    returns = pd.Series([0.02, -0.02, 0.02, -0.02])
    sample_std = math.sqrt((4 * 0.02**2) / 3)
    expected = sample_std * math.sqrt(4)
    assert stats.annualized_vol(returns, periods_per_year=4) == pytest.approx(expected)


def test_sharpe_zero_mean_is_zero() -> None:
    returns = pd.Series([0.02, -0.02, 0.02, -0.02])
    assert stats.sharpe_ratio(returns, periods_per_year=4) == pytest.approx(0.0)


def test_sharpe_hand_computed() -> None:
    returns = pd.Series([0.03, 0.01, 0.03, 0.01])
    mean = 0.02
    # deviations from the mean are +-0.01, all four periods -> sum of
    # squares is 4 * 0.01**2, not 2 * 0.01**2.
    sample_std = math.sqrt((4 * 0.01**2) / 3)
    expected = mean / sample_std * math.sqrt(4)
    assert stats.sharpe_ratio(returns, periods_per_year=4) == pytest.approx(expected)


def test_sharpe_nan_on_zero_vol() -> None:
    returns = pd.Series([0.01, 0.01, 0.01])
    assert math.isnan(stats.sharpe_ratio(returns, periods_per_year=252))


def test_sortino_only_penalizes_downside() -> None:
    # All-positive returns: no downside deviation -> NaN (undefined), not zero.
    returns = pd.Series([0.01, 0.02, 0.01, 0.02])
    assert math.isnan(stats.sortino_ratio(returns, periods_per_year=4))


def test_sortino_hand_computed() -> None:
    returns = pd.Series([0.04, -0.02, 0.04, -0.02])
    mean = 0.01
    # downside deviation over ALL periods: positive periods contribute 0
    downside_dev = math.sqrt((2 * 0.02**2) / 4)
    expected = mean / downside_dev * math.sqrt(4)
    assert stats.sortino_ratio(returns, periods_per_year=4) == pytest.approx(expected)


def test_max_drawdown_hand_computed() -> None:
    curve = pd.Series([100.0, 110.0, 90.0, 95.0, 120.0])
    dd = stats.max_drawdown(curve)
    assert dd.max_drawdown == pytest.approx(90.0 / 110.0 - 1.0)
    assert dd.peak_index == 1
    assert dd.trough_index == 2
    assert dd.recovery_index == 4
    assert dd.duration_bars == 3


def test_max_drawdown_never_recovers() -> None:
    curve = pd.Series([100.0, 110.0, 90.0, 95.0])
    dd = stats.max_drawdown(curve)
    assert dd.recovery_index is None
    assert dd.duration_bars == 2  # peak (idx 1) to end (idx 3)


def test_max_drawdown_monotonic_up_is_zero() -> None:
    curve = pd.Series([100.0, 110.0, 120.0])
    dd = stats.max_drawdown(curve)
    assert dd.max_drawdown == pytest.approx(0.0)


def test_calmar_hand_computed() -> None:
    assert stats.calmar_ratio(0.21, -0.1818) == pytest.approx(0.21 / 0.1818, rel=1e-3)


def test_calmar_nan_with_no_drawdown() -> None:
    assert math.isnan(stats.calmar_ratio(0.1, 0.0))


def test_turnover_hand_computed() -> None:
    weights = pd.DataFrame({"a": [0.5, 0.6, 0.6], "b": [0.5, 0.4, 0.3]})
    # bar1->bar2: |0.1| + |0.1| = 0.2 ; bar2->bar3: |0| + |0.1| = 0.1
    assert stats.turnover(weights) == pytest.approx((0.2 + 0.1) / 2)


def test_exposure_hand_computed() -> None:
    weights = pd.DataFrame({"a": [0.5, 0.6], "b": [-0.3, 0.1]})
    # bar1: 0.8, bar2: 0.7
    assert stats.exposure(weights) == pytest.approx((0.8 + 0.7) / 2)


def test_hit_rate_and_avg_win_loss_hand_computed() -> None:
    trades = pd.Series([0.05, -0.02, 0.03, -0.01, 0.0])
    assert stats.hit_rate(trades) == pytest.approx(2 / 5)
    win_loss = stats.avg_win_loss(trades)
    assert win_loss.avg_win == pytest.approx((0.05 + 0.03) / 2)
    assert win_loss.avg_loss == pytest.approx((-0.02 - 0.01) / 2)


def test_hit_rate_nan_with_no_trades() -> None:
    assert math.isnan(stats.hit_rate(pd.Series([], dtype=float)))


def test_avg_win_loss_nan_when_no_losses() -> None:
    win_loss = stats.avg_win_loss(pd.Series([0.01, 0.02]))
    assert math.isnan(win_loss.avg_loss)


def test_cost_drag_hand_computed() -> None:
    gross = pd.Series([0.01, 0.01])
    net = pd.Series([0.005, 0.005])
    drag = stats.cost_drag(gross, net, periods_per_year=2)
    gross_curve = pd.Series([1.01, 1.01**2])
    net_curve = pd.Series([1.005, 1.005**2])
    expected = stats.cagr(gross_curve, 2) - stats.cagr(net_curve, 2)
    assert drag == pytest.approx(expected)
    assert drag > 0  # costs should reduce annualised return


def test_break_even_cost_hand_computed() -> None:
    gross = pd.Series([0.02, 0.02, 0.02])
    # mean gross = 0.02, turnover/bar = 0.5 -> break-even cost = 0.04 (4% per trade)
    assert stats.break_even_cost(
        gross, turnover_per_bar=0.5, periods_per_year=252
    ) == pytest.approx(0.04)


def test_break_even_cost_zero_when_gross_already_negative() -> None:
    gross = pd.Series([-0.01, -0.01])
    assert stats.break_even_cost(gross, turnover_per_bar=0.5, periods_per_year=252) == 0.0


def test_summarize_bundles_everything() -> None:
    curve = pd.Series([100.0, 110.0, 121.0])
    returns = curve.pct_change().dropna()
    weights = pd.DataFrame({"a": [0.5, 0.5, 0.5]})
    trades = pd.Series([0.05, 0.03])
    summary = stats.summarize(curve, returns, weights, trades, periods_per_year=252)
    assert summary.cagr == pytest.approx(stats.cagr(curve, 252))
    assert summary.hit_rate == pytest.approx(1.0)
