"""DSR / PSR / PBO / bootstrap against hand-worked values."""

from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np
import pandas as pd
import pytest

from research import overfitting

_N = NormalDist()


def test_psr_matches_hand_computation_for_two_point_distribution() -> None:
    """A two-point distribution has skew 0 and kurtosis exactly 1, so
    the PSR denominator is sqrt(1 + (1-1)/4*SR^2) = 1 and
    PSR = Phi(SR * sqrt(T - 1))."""
    r = np.array([0.02, -0.01] * 50)
    sr = r.mean() / r.std(ddof=1)
    expected = _N.cdf(sr * math.sqrt(len(r) - 1))
    assert overfitting.probabilistic_sharpe_ratio(r) == pytest.approx(expected, rel=1e-12)


def test_expected_max_sharpe_hand_computed() -> None:
    # N=10, V=1: (1-g)*Z^-1(0.9) + g*Z^-1(1 - 1/(10e))
    g = overfitting.EULER_MASCHERONI
    expected = (1 - g) * _N.inv_cdf(0.9) + g * _N.inv_cdf(1 - 1 / (10 * math.e))
    assert expected == pytest.approx(1.5747, abs=1e-3)
    assert overfitting.expected_max_sharpe(10, 1.0) == pytest.approx(expected)
    # Scales with the standard deviation of trial Sharpes.
    assert overfitting.expected_max_sharpe(10, 0.04) == pytest.approx(0.2 * expected)


def test_single_trial_has_no_selection_hurdle() -> None:
    assert overfitting.expected_max_sharpe(1, 1.0) == 0.0
    r = np.random.default_rng(0).normal(0.001, 0.01, 500)
    dsr = overfitting.deflated_sharpe_ratio(r, 1, 0.5)
    assert dsr.dsr == pytest.approx(overfitting.probabilistic_sharpe_ratio(r, 0.0))


def test_more_trials_lower_the_dsr() -> None:
    r = np.random.default_rng(1).normal(0.001, 0.01, 1000)
    few = overfitting.deflated_sharpe_ratio(r, 5, 0.001).dsr
    many = overfitting.deflated_sharpe_ratio(r, 500, 0.001).dsr
    assert many < few


def test_pbo_zero_when_one_trial_dominates_everywhere() -> None:
    rng = np.random.default_rng(2)
    noise = rng.normal(0, 0.01, (800, 6))
    noise[:, 0] += 0.01  # trial 0 is better in every block
    pbo = overfitting.probability_of_backtest_overfitting(pd.DataFrame(noise), n_blocks=8)
    assert pbo.pbo == 0.0
    assert pbo.n_splits == math.comb(8, 4)


def test_pbo_near_half_for_pure_noise() -> None:
    rng = np.random.default_rng(3)
    noise = pd.DataFrame(rng.normal(0, 0.01, (2000, 20)))
    pbo = overfitting.probability_of_backtest_overfitting(noise, n_blocks=10)
    assert 0.25 < pbo.pbo < 0.75


def test_pbo_high_when_in_sample_winner_is_out_of_sample_loser() -> None:
    """Each trial is great in exactly one block and bad elsewhere -- the
    textbook overfit: whatever wins in-sample won by being lucky in a
    block that isn't out-of-sample."""
    n_trials, n_blocks, block_len = 8, 8, 50
    m = np.full((n_blocks * block_len, n_trials), -0.001)
    rng = np.random.default_rng(4)
    m += rng.normal(0, 0.0001, m.shape)
    for j in range(n_trials):
        m[j * block_len : (j + 1) * block_len, j] = 0.01
    pbo = overfitting.probability_of_backtest_overfitting(pd.DataFrame(m), n_blocks=n_blocks)
    assert pbo.pbo > 0.9


def test_bootstrap_ci_brackets_point_estimate_and_is_reproducible() -> None:
    r = pd.Series(np.random.default_rng(5).normal(0.0005, 0.01, 1500))
    a = overfitting.block_bootstrap_sharpe_ci(r, 252, block_length=20, n_resamples=500, seed=7)
    b = overfitting.block_bootstrap_sharpe_ci(r, 252, block_length=20, n_resamples=500, seed=7)
    assert a == b
    assert a.lower < a.sharpe < a.upper
    assert a.sharpe == pytest.approx(r.mean() / r.std(ddof=1) * math.sqrt(252))


def test_bootstrap_ci_nan_when_series_too_short() -> None:
    ci = overfitting.block_bootstrap_sharpe_ci(np.array([0.01, 0.02, -0.01]), 252, 20)
    assert math.isnan(ci.lower) and math.isnan(ci.upper)
