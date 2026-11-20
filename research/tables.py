"""Per-year and per-regime breakdowns (spec Section 7, step 4).

**Regimes** are labelled from the benchmark's own trailing trend, using
only information available at each bar: price above a rising 200-day
SMA = bull, below a falling one = bear, anything else = sideways. A
strategy that only makes money in one regime is a regime bet, not an
edge — this table is where that shows up.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from research import stats


def _period_stats(returns: pd.Series, periods_per_year: float) -> dict[str, float]:
    if len(returns) < 2:
        return {
            "bars": len(returns),
            "return": float("nan"),
            "vol": float("nan"),
            "sharpe": float("nan"),
            "max_drawdown": float("nan"),
        }
    curve = (1.0 + returns).cumprod()
    return {
        "bars": len(returns),
        "return": float(curve.iloc[-1] - 1.0),
        "vol": stats.annualized_vol(returns, periods_per_year),
        "sharpe": stats.sharpe_ratio(returns, periods_per_year),
        "max_drawdown": stats.max_drawdown(
            pd.concat([pd.Series([1.0]), curve.reset_index(drop=True)])
        ).max_drawdown,
    }


def per_year(returns: pd.Series, periods_per_year: float) -> dict[str, dict[str, float]]:
    idx = pd.DatetimeIndex(returns.index)
    return {
        str(year): _period_stats(returns[idx.year == year], periods_per_year)
        for year in sorted(set(idx.year))
    }


def regime_labels(benchmark_close: pd.Series, sma_bars: int, slope_bars: int) -> pd.Series:
    """bull / bear / sideways per bar, causal (rolling windows end at the bar)."""
    sma = benchmark_close.rolling(sma_bars, min_periods=sma_bars).mean()
    slope = sma - sma.shift(slope_bars)
    above = benchmark_close > sma
    label = pd.Series("sideways", index=benchmark_close.index)
    label[(above) & (slope > 0)] = "bull"
    label[(~above) & (slope < 0) & sma.notna()] = "bear"
    label[sma.isna()] = "unknown"
    return label


def per_regime(
    returns: pd.Series, labels: pd.Series, periods_per_year: float
) -> dict[str, dict[str, float]]:
    """Stats per regime. Returns earned at bar t are attributed to the
    regime known at bar t-1's close (when the position was chosen)."""
    lagged = labels.shift(1).reindex(returns.index)
    out: dict[str, dict[str, float]] = {}
    for regime in ("bull", "sideways", "bear"):
        r = returns[lagged == regime]
        s = _period_stats(r, periods_per_year)
        s["share_of_bars"] = float(len(r) / len(returns)) if len(returns) else float("nan")
        # mean per-bar return annualised: comparable across regimes of different length
        s["ann_mean_return"] = float(r.mean() * periods_per_year) if len(r) else float("nan")
        out[regime] = s
    return out


def drawdown_series(equity: pd.Series) -> pd.Series:
    peak = equity.cummax()
    return equity / peak - 1.0


def clean(x: object) -> object:
    """JSON-safe: NaN/inf -> None, numpy scalars -> Python, recursively."""
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [clean(v) for v in x]
    if isinstance(x, np.floating | float):
        f = float(x)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, np.bool_):
        return bool(x)
    if isinstance(x, pd.Timestamp):
        return x.isoformat()
    return x
