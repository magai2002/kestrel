"""Performance statistics (spec Section 7, step 4 — the basic list;
Deflated Sharpe Ratio and Probability of Backtest Overfitting need the
trial ledger and arrive with the rest of the validation pipeline in
Phase 3, as do the bootstrap CI, cost-sensitivity sweep, per-year/
per-regime tables and the parameter-stability heatmap, all of which
depend on walk-forward windows that don't exist yet).

**The annualisation trap** (demonstrated in
``notebooks/01_first_look.ipynb``): there is no one "annualization
factor." Crypto trades every calendar day; US equities trade ~252 days
a year. Every function here takes an explicit ``periods_per_year``
rather than assuming one, specifically so a caller can't silently reuse
the equities convention (252) on a crypto series that actually has 365
trading days (or 365x6 four-hour bars) — which would understate crypto's
true annualised volatility and overstate its Sharpe ratio.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd


def total_return(equity_curve: pd.Series) -> float:
    """Fractional return from the first to the last point of an equity curve."""
    if len(equity_curve) < 2:
        return 0.0
    return float(equity_curve.iloc[-1] / equity_curve.iloc[0] - 1)


def cagr(equity_curve: pd.Series, periods_per_year: float) -> float:
    """Compound annual growth rate implied by an equity curve.

    ``periods_per_year`` converts bar count to years — 365 (or
    365 * bars_per_day for sub-daily crypto bars) for crypto, 252 for
    equities. NaN if the curve is too short or ever non-positive
    (CAGR on a path that hit zero or went negative isn't a real number).
    """
    n_periods = len(equity_curve) - 1
    if n_periods <= 0:
        return 0.0
    start, end = equity_curve.iloc[0], equity_curve.iloc[-1]
    if start <= 0 or end <= 0:
        return float("nan")
    years = n_periods / periods_per_year
    return float((end / start) ** (1.0 / years) - 1.0)


def annualized_vol(returns: pd.Series, periods_per_year: float) -> float:
    """Annualised standard deviation of per-bar returns."""
    if len(returns) < 2:
        return float("nan")
    return float(returns.std(ddof=1) * math.sqrt(periods_per_year))


def sharpe_ratio(returns: pd.Series, periods_per_year: float, risk_free: float = 0.0) -> float:
    """Annualised Sharpe ratio. ``risk_free`` is an annual rate."""
    if len(returns) < 2:
        return float("nan")
    excess = returns - risk_free / periods_per_year
    vol = excess.std(ddof=1)
    if vol == 0 or np.isnan(vol):
        return float("nan")
    return float(excess.mean() / vol * math.sqrt(periods_per_year))


def sortino_ratio(returns: pd.Series, periods_per_year: float, risk_free: float = 0.0) -> float:
    """Annualised Sortino ratio: like Sharpe, but only penalises downside
    deviation (returns below the risk-free rate), not upside volatility.

    Downside deviation is computed over *all* periods (not just the
    losing ones) — a period with a positive excess return contributes
    zero to it, which is the standard definition; computing it only
    over losing periods would ignore how often downside happens at all.
    """
    if len(returns) < 2:
        return float("nan")
    excess = returns - risk_free / periods_per_year
    downside_sq = excess.clip(upper=0.0) ** 2
    downside_dev = math.sqrt(downside_sq.mean())
    if downside_dev == 0:
        return float("nan")
    return float(excess.mean() / downside_dev * math.sqrt(periods_per_year))


@dataclass(frozen=True)
class DrawdownStats:
    max_drawdown: float  # negative fraction, e.g. -0.35 for a 35% drawdown
    peak_index: object
    trough_index: object
    recovery_index: object | None  # None if never recovered within the curve
    duration_bars: int  # peak -> recovery (or peak -> end, if unrecovered)


def max_drawdown(equity_curve: pd.Series) -> DrawdownStats:
    """Deepest peak-to-trough decline, plus how long it took to recover.

    Worked out positionally (numpy arrays), not via pandas label-based
    ``.loc``: the equity curve's index type is whatever the caller used
    (int, Timestamp, ...), and duration is a bar count regardless —
    positional math sidesteps both the ambiguity and a string of
    pandas-stubs overload mismatches ``.loc[label]``/``.loc[:label]``
    runs into when the label type isn't known statically.
    """
    if len(equity_curve) < 2:
        idx = equity_curve.index[0] if len(equity_curve) else None
        return DrawdownStats(0.0, idx, idx, idx, 0)

    values = equity_curve.to_numpy()
    running_max = np.maximum.accumulate(values)
    drawdown = values / running_max - 1.0
    trough_pos = int(np.argmin(drawdown))
    max_dd = float(drawdown[trough_pos])

    peak_pos = int(np.argmax(values[: trough_pos + 1]))
    peak_value = values[peak_pos]

    after_trough = values[trough_pos:]
    recovered_offsets = np.flatnonzero(after_trough >= peak_value)
    recovery_pos = trough_pos + int(recovered_offsets[0]) if len(recovered_offsets) else None

    end_pos = recovery_pos if recovery_pos is not None else len(values) - 1
    duration_bars = end_pos - peak_pos

    index = equity_curve.index
    return DrawdownStats(
        max_drawdown=max_dd,
        peak_index=index[peak_pos],
        trough_index=index[trough_pos],
        recovery_index=index[recovery_pos] if recovery_pos is not None else None,
        duration_bars=int(duration_bars),
    )


def calmar_ratio(cagr_value: float, max_dd: float) -> float:
    """CAGR divided by the magnitude of the max drawdown. NaN if there was none."""
    if max_dd == 0 or np.isnan(max_dd):
        return float("nan")
    return float(cagr_value / abs(max_dd))


def turnover(weights: pd.DataFrame) -> float:
    """Average per-bar turnover: mean of sum(|weight_t - weight_{t-1}|) across instruments.

    This is exactly what position buffering (spec Section 5) is meant
    to reduce — pass the same weights through
    ``research.vectorized.apply_buffer`` and compare.
    """
    if len(weights) < 2:
        return 0.0
    # .iloc[1:] drops the first (all-NaN) diff row explicitly: pandas'
    # DataFrame.sum(axis=1, skipna=True) treats an all-NaN row as 0, not
    # NaN, so without this the undefined first-bar transition would
    # silently count as a real zero-turnover bar and bias the average down.
    diffs = weights.diff().iloc[1:]
    return float(diffs.abs().sum(axis=1).mean())


def exposure(weights: pd.DataFrame) -> float:
    """Average gross exposure: mean of sum(|weight|) across instruments per bar."""
    if weights.empty:
        return 0.0
    return float(weights.abs().sum(axis=1).mean())


def hit_rate(trade_returns: pd.Series) -> float:
    """Fraction of trades with a positive return. NaN if there were no trades."""
    if len(trade_returns) == 0:
        return float("nan")
    return float((trade_returns > 0).mean())


@dataclass(frozen=True)
class WinLoss:
    avg_win: float
    avg_loss: float  # negative, or NaN if there were no losing trades


def avg_win_loss(trade_returns: pd.Series) -> WinLoss:
    wins = trade_returns[trade_returns > 0]
    losses = trade_returns[trade_returns < 0]
    avg_win = float(wins.mean()) if len(wins) else float("nan")
    avg_loss = float(losses.mean()) if len(losses) else float("nan")
    return WinLoss(avg_win, avg_loss)


def cost_drag(gross_returns: pd.Series, net_returns: pd.Series, periods_per_year: float) -> float:
    """Annualised return given up to costs: CAGR(gross) - CAGR(net).

    Both return series are converted to equity curves starting at 1.0
    so the comparison is apples-to-apples regardless of how each was
    produced.
    """
    gross_curve = (1.0 + gross_returns).cumprod()
    net_curve = (1.0 + net_returns).cumprod()
    return cagr(gross_curve, periods_per_year) - cagr(net_curve, periods_per_year)


def break_even_cost(
    gross_returns: pd.Series, turnover_per_bar: float, periods_per_year: float
) -> float:
    """The per-trade cost (as a fraction of notional) at which net Sharpe hits zero.

    Approximates net return as gross return minus (turnover * cost per
    bar), then solves for the cost where the resulting Sharpe ratio is
    zero — i.e. where mean excess return hits zero, since Sharpe's sign
    only depends on the numerator once volatility is positive. If gross
    mean return is already <= 0, costs can only make it worse, so the
    break-even cost is 0 (undefined/negative in theory, reported as 0
    rather than a nonsensical negative "cost").
    """
    if turnover_per_bar <= 0:
        return float("nan")
    mean_gross = float(gross_returns.mean())
    if mean_gross <= 0:
        return 0.0
    return mean_gross / turnover_per_bar


@dataclass(frozen=True)
class SummaryStats:
    """Everything in this module, bundled — what a backtest report shows."""

    cagr: float
    annualized_vol: float
    sharpe: float
    sortino: float
    calmar: float
    max_drawdown: float
    max_drawdown_duration_bars: int
    turnover: float
    exposure: float
    hit_rate: float
    avg_win: float
    avg_loss: float


def summarize(
    equity_curve: pd.Series,
    returns: pd.Series,
    weights: pd.DataFrame,
    trade_returns: pd.Series,
    periods_per_year: float,
    risk_free: float = 0.0,
) -> SummaryStats:
    """Convenience wrapper: every basic metric for one backtest run."""
    cagr_value = cagr(equity_curve, periods_per_year)
    dd = max_drawdown(equity_curve)
    win_loss = avg_win_loss(trade_returns)
    return SummaryStats(
        cagr=cagr_value,
        annualized_vol=annualized_vol(returns, periods_per_year),
        sharpe=sharpe_ratio(returns, periods_per_year, risk_free),
        sortino=sortino_ratio(returns, periods_per_year, risk_free),
        calmar=calmar_ratio(cagr_value, dd.max_drawdown),
        max_drawdown=dd.max_drawdown,
        max_drawdown_duration_bars=dd.duration_bars,
        turnover=turnover(weights),
        exposure=exposure(weights),
        hit_rate=hit_rate(trade_returns),
        avg_win=win_loss.avg_win,
        avg_loss=win_loss.avg_loss,
    )
