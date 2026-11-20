"""Overfitting statistics (spec Section 7, step 4): Probabilistic and
Deflated Sharpe Ratio, Probability of Backtest Overfitting via
combinatorially symmetric cross-validation (CSCV), and a block-bootstrap
confidence interval for Sharpe.

All implemented from the papers' formulas with the standard library's
``statistics.NormalDist`` — no scipy dependency for two functions.
Every formula is unit-tested against a hand-worked value
(``tests/unit/research/test_overfitting.py``).

**Sharpe ratios here are per-period (not annualised).** The DSR/PSR
formulas are derived for the per-observation Sharpe and the number of
observations ``T``; annualising first and then plugging into the same
formula would be wrong by a factor of sqrt(periods_per_year).

References:
- Bailey & López de Prado (2012), "The Sharpe Ratio Efficient Frontier"
  (Probabilistic Sharpe Ratio).
- Bailey & López de Prado (2014), "The Deflated Sharpe Ratio: Correcting
  for Selection Bias, Backtest Overfitting and Non-Normality".
- Bailey, Borwein, López de Prado & Zhu (2015), "The Probability of
  Backtest Overfitting" (CSCV).
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np
import pandas as pd

_N = NormalDist()
EULER_MASCHERONI = 0.5772156649015329


def per_period_sharpe(returns: pd.Series | np.ndarray) -> float:
    r = np.asarray(returns, dtype=float)
    if len(r) < 2:
        return float("nan")
    sd = r.std(ddof=1)
    if sd == 0 or not np.isfinite(sd):
        return float("nan")
    return float(r.mean() / sd)


def _skew_kurt(returns: np.ndarray) -> tuple[float, float]:
    """Sample skewness and (non-excess) kurtosis, population moments.
    A normal distribution gives (0, 3)."""
    r = returns - returns.mean()
    m2 = float((r**2).mean())
    if m2 == 0:
        return 0.0, 3.0
    skew = float((r**3).mean() / m2**1.5)
    kurt = float((r**4).mean() / m2**2)
    return skew, kurt


def probabilistic_sharpe_ratio(returns: pd.Series | np.ndarray, benchmark_sr: float = 0.0) -> float:
    """P(true Sharpe > ``benchmark_sr``) given the observed returns,
    correcting for sample length, skewness and fat tails:

        PSR = Phi( (SR - SR*) * sqrt(T - 1) / sqrt(1 - g3*SR + (g4 - 1)/4 * SR^2) )
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    t = len(r)
    sr = per_period_sharpe(r)
    if t < 3 or not np.isfinite(sr):
        return float("nan")
    skew, kurt = _skew_kurt(r)
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr**2
    if denom <= 0:
        return float("nan")
    z = (sr - benchmark_sr) * math.sqrt(t - 1) / math.sqrt(denom)
    return _N.cdf(z)


def expected_max_sharpe(n_trials: int, sharpe_variance: float) -> float:
    """The Sharpe you'd expect from the *best* of ``n_trials`` strategies
    that all have true Sharpe zero — the DSR's "hurdle":

        SR0 = sqrt(V) * ( (1 - g) * Z^-1(1 - 1/N) + g * Z^-1(1 - 1/(N e)) )

    where g is the Euler-Mascheroni constant. With one trial there is no
    selection, so the hurdle is 0.
    """
    if n_trials <= 1 or sharpe_variance <= 0 or not np.isfinite(sharpe_variance):
        return 0.0
    g = EULER_MASCHERONI
    z1 = _N.inv_cdf(1.0 - 1.0 / n_trials)
    z2 = _N.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(sharpe_variance) * ((1.0 - g) * z1 + g * z2)


@dataclass(frozen=True)
class DeflatedSharpe:
    dsr: float  # probability the true Sharpe beats the selection hurdle
    observed_sr: float  # per-period
    hurdle_sr: float  # per-period SR0
    n_trials: int
    sharpe_variance: float


def deflated_sharpe_ratio(
    returns: pd.Series | np.ndarray, n_trials: int, trial_sharpe_variance: float
) -> DeflatedSharpe:
    """PSR evaluated against the expected-max-Sharpe hurdle of ``n_trials``
    (Bailey & López de Prado 2014). ``trial_sharpe_variance`` is the
    variance of the *per-period* Sharpe across all trials tried (from the
    trial ledger)."""
    hurdle = expected_max_sharpe(n_trials, trial_sharpe_variance)
    return DeflatedSharpe(
        dsr=probabilistic_sharpe_ratio(returns, hurdle),
        observed_sr=per_period_sharpe(returns),
        hurdle_sr=hurdle,
        n_trials=n_trials,
        sharpe_variance=trial_sharpe_variance,
    )


@dataclass(frozen=True)
class PBOResult:
    pbo: float  # fraction of splits where the in-sample winner is below median out-of-sample
    logits: list[float]
    n_splits: int
    n_trials: int


def probability_of_backtest_overfitting(
    trial_returns: pd.DataFrame, n_blocks: int = 16
) -> PBOResult:
    """CSCV (Bailey et al. 2015). ``trial_returns``: rows = bars, columns
    = trials (one per parameter combination), same period for all.

    Split the rows into ``n_blocks`` contiguous blocks; for every way of
    choosing half of them as "in-sample" (the rest out-of-sample): pick
    the trial with the best in-sample Sharpe, find its relative rank
    ``w`` among all trials out-of-sample, and record ``logit(w)``. PBO is
    the fraction of splits where that logit is <= 0 — i.e. the
    in-sample winner did no better than the median trial out of sample.
    ~0.5 or above means the selection process is no better than chance.
    """
    m = trial_returns.dropna(how="any").to_numpy(dtype=float)
    n_rows, n_trials = m.shape
    if n_trials < 2:
        return PBOResult(float("nan"), [], 0, n_trials)
    n_blocks = min(n_blocks, n_rows // 2 * 2)
    if n_blocks < 2:
        return PBOResult(float("nan"), [], 0, n_trials)
    if n_blocks % 2:
        n_blocks -= 1
    blocks = np.array_split(np.arange(n_rows), n_blocks)

    # Per-block sums/sums-of-squares make each split's Sharpe O(blocks), not O(rows).
    sums = np.stack([m[b].sum(axis=0) for b in blocks])
    sq = np.stack([(m[b] ** 2).sum(axis=0) for b in blocks])
    counts = np.array([len(b) for b in blocks], dtype=float)

    def sharpe_from(idx: tuple[int, ...]) -> np.ndarray:
        n = counts[list(idx)].sum()
        s = sums[list(idx)].sum(axis=0)
        s2 = sq[list(idx)].sum(axis=0)
        mean = s / n
        var = (s2 - n * mean**2) / max(n - 1, 1)
        sd = np.sqrt(np.clip(var, 0, None))
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(sd > 0, mean / sd, 0.0)

    logits: list[float] = []
    all_blocks = set(range(n_blocks))
    for in_sample in itertools.combinations(range(n_blocks), n_blocks // 2):
        out_sample = tuple(sorted(all_blocks - set(in_sample)))
        is_sr = sharpe_from(in_sample)
        oos_sr = sharpe_from(out_sample)
        best = int(np.argmax(is_sr))
        # Relative rank in (0, 1): 1 = best out-of-sample.
        rank = (oos_sr < oos_sr[best]).sum() + 0.5 * ((oos_sr == oos_sr[best]).sum() - 1) + 1
        w = rank / (n_trials + 1)
        logits.append(math.log(w / (1 - w)))
    pbo = float(np.mean([lg <= 0 for lg in logits]))
    return PBOResult(pbo, logits, len(logits), n_trials)


@dataclass(frozen=True)
class BootstrapCI:
    sharpe: float  # annualised point estimate
    lower: float
    upper: float
    confidence: float
    block_length: int
    n_resamples: int


def block_bootstrap_sharpe_ci(
    returns: pd.Series | np.ndarray,
    periods_per_year: float,
    block_length: int,
    n_resamples: int = 2000,
    confidence: float = 0.95,
    seed: int = 0,
) -> BootstrapCI:
    """Moving-block bootstrap CI for the annualised Sharpe.

    Resampling *blocks* of consecutive returns (not individual bars)
    keeps the volatility clustering and autocorrelation that a plain
    i.i.d. bootstrap would destroy — which matters, because those make
    the i.i.d. interval too narrow. Seeded, so the report is reproducible.
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    n = len(r)
    ann = math.sqrt(periods_per_year)
    point = per_period_sharpe(r) * ann
    if n < 2 * block_length or block_length < 1:
        return BootstrapCI(point, float("nan"), float("nan"), confidence, block_length, 0)
    rng = np.random.default_rng(seed)
    n_blocks = math.ceil(n / block_length)
    starts = rng.integers(0, n - block_length + 1, size=(n_resamples, n_blocks))
    offsets = np.arange(block_length)
    idx = (starts[:, :, None] + offsets[None, None, :]).reshape(n_resamples, -1)[:, :n]
    samples = r[idx]
    sd = samples.std(axis=1, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        sharpes = np.where(sd > 0, samples.mean(axis=1) / sd, np.nan) * ann
    sharpes = sharpes[np.isfinite(sharpes)]
    alpha = (1 - confidence) / 2
    return BootstrapCI(
        sharpe=point,
        lower=float(np.quantile(sharpes, alpha)),
        upper=float(np.quantile(sharpes, 1 - alpha)),
        confidence=confidence,
        block_length=block_length,
        n_resamples=n_resamples,
    )
