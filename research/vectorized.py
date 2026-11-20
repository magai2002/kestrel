"""The vectorized quick backtester.

Given a price panel and a strategy's target-weight panel, computes
returns, equity curve and turnover — no order objects, no broker, no
event bus, just weights times returns. Separate on purpose from the
event-driven engine (``backtest/``, Phase 2): this one is fast because
it does almost nothing, which is exactly what Phase 3's walk-forward
parameter grid search needs, and the event engine's results must match
this one within a stated tolerance (the parity test).

**No look-ahead by construction** (design rule 3): a weight decided
using bar *t*'s close can only earn the return from bar *t* to bar
*t+1* — ``target_weights`` is shifted forward one bar before being
multiplied by instrument returns. The same shift controls when a
trade's cost is charged: a trade that changes the held weight from
``buffered[t-1]`` to ``buffered[t]`` is what makes ``buffered[t]`` the
position earning bar *t+1*'s return, so its cost is charged against
bar *t+1* too — the same bar as the return it enabled, not the bar
before. Getting this one bar off would make net returns look wrong
relative to gross (a cost appearing before the trade that caused it
even takes effect), silently corrupting cost-drag and break-even-cost
numbers.

**Known limitation: turnover is measured in weight-change, not actual
trades.** This mischarges a strategy whose target weight *passively
drifts* with price (``strategies.baselines.BuyAndHold`` is exactly
this) as if it were trading every bar, when in reality holding a fixed
quantity needs no trade at all — the weight only looks like it's
changing because the portfolio's own value is changing underneath it.
The real system doesn't have this problem: the portfolio layer
converts weight to a target *quantity*, and the order manager only
trades a quantity difference (design rule 1), so a passively-drifting
weight that implies a constant quantity correctly produces zero orders
there. Fixing it here would mean this module tracking quantities too,
which is most of what makes the event-driven engine more than "weights
times returns" in the first place — not worth it for a tool whose job
is to be fast and approximate. Known, accepted, and the reason
Phase 2's parity tests use a strategy with genuine turnover, not
``BuyAndHold``, as their comparison case.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from execution.costs import CostModel


def apply_buffer(weights: pd.DataFrame, buffer_pct: float) -> pd.DataFrame:
    """Position buffering (spec Section 5): hold the previous weight
    unless the new target differs from it by more than ``buffer_pct``.

    Sequential by nature — bar *t*'s held weight depends on whether bar
    *t-1* traded, which depends on bar *t-2*'s held weight, and so on —
    so this loops over rows rather than being a single vector op. The
    loop runs on a raw numpy array: the original pandas-per-row version
    (``Series.where`` + ``.iloc`` assignment each bar) made a Phase 3
    walk-forward over 17k crypto bars take minutes, all of it pandas
    indexing overhead. Same semantics, including NaN (a NaN difference
    never counts as "beyond the buffer").
    """
    if weights.empty:
        return weights.copy()
    if buffer_pct <= 0:
        return weights.copy()

    values = weights.to_numpy(dtype=float, copy=True)
    held = values[0].copy()
    with np.errstate(invalid="ignore"):
        for i in range(1, len(values)):
            target = values[i]
            trade = np.abs(target - held) > buffer_pct
            held = np.where(trade, target, held)
            values[i] = held
    return pd.DataFrame(values, index=weights.index, columns=weights.columns)


@dataclass(frozen=True)
class VectorizedResult:
    equity_curve: pd.Series  # starts at 1.0
    net_returns: pd.Series
    gross_returns: pd.Series
    applied_weights: pd.DataFrame  # the actually-held, shifted, buffered weights
    raw_weights: pd.DataFrame  # the strategy's unbuffered, unshifted target weights
    turnover_per_bar: pd.Series


def run(
    prices: pd.DataFrame,
    target_weights: pd.DataFrame,
    buffer_pct: float = 0.0,
    cost_bps: float = 0.0,
) -> VectorizedResult:
    """Run the vectorized backtest.

    ``prices`` and ``target_weights`` share an index (bar close times)
    and columns (instrument ids). ``cost_bps`` is a single flat
    round-trip cost per unit of turnover, in basis points — a
    deliberate simplification versus the event engine's full cost model
    (``execution/costs.py``, Phase 2), good enough for a first look and
    for the walk-forward grid search where speed matters more than
    cost-model fidelity.
    """
    buffered = apply_buffer(target_weights, buffer_pct)
    applied_weights = buffered.shift(1).fillna(0.0)

    instrument_returns = prices.pct_change().fillna(0.0)
    gross_returns = (applied_weights * instrument_returns).sum(axis=1)

    # Charged in the same bar as the return the trade enabled -- see
    # module docstring for why this isn't `buffered.diff()` directly.
    turnover_per_bar = applied_weights.diff().abs().sum(axis=1).fillna(0.0)
    cost_per_bar = turnover_per_bar * (cost_bps / 10_000.0)
    net_returns = gross_returns - cost_per_bar

    equity_curve = (1.0 + net_returns).cumprod()

    return VectorizedResult(
        equity_curve=equity_curve,
        net_returns=net_returns,
        gross_returns=gross_returns,
        applied_weights=applied_weights,
        raw_weights=target_weights,
        turnover_per_bar=turnover_per_bar,
    )


def trailing_vol(prices: pd.DataFrame, span_bars: int) -> pd.DataFrame:
    """Per-bar EWM vol of returns, shifted one bar (same causality rule
    as ``backtest.engine._trailing_volatility``)."""
    return prices.pct_change().ewm(span=span_bars, min_periods=span_bars).std().shift(1).fillna(0.0)


def run_with_costs(
    prices: pd.DataFrame,
    target_weights: pd.DataFrame,
    cost_models: dict[str, CostModel],
    initial_capital: float,
    buffer_pct: float = 0.0,
    vol: pd.DataFrame | None = None,
    cost_multiplier: float = 1.0,
) -> VectorizedResult:
    """Like ``run``, but each trade is charged the same cost model the
    event engine uses (``execution/costs.py``): commission **with its
    per-order minimum and fixed fee**, TOB, half-spread and
    volatility-scaled slippage, all scaled by ``cost_multiplier`` (the
    0x/1x/2x/3x cost-sensitivity sweep).

    The per-order minimum is why this can't be one vector op: whether a
    $1.70 minimum dominates depends on the trade's *dollar* size, which
    depends on equity, which depends on every earlier bar's costs. So
    it's a loop over bars (numpy inside) — still fast enough for a
    grid search over thousands of bars.
    """
    columns = list(prices.columns)
    buffered = apply_buffer(target_weights[columns], buffer_pct)
    applied = buffered.shift(1).fillna(0.0)
    rets = prices.pct_change().fillna(0.0).to_numpy()
    w = applied.to_numpy()
    vol_arr = (
        vol[columns] if vol is not None else pd.DataFrame(0.0, prices.index, columns)
    ).to_numpy()

    def arr(attr: str) -> np.ndarray:
        return np.array([float(getattr(cost_models[c], attr)) for c in columns]) * cost_multiplier

    fee, min_fee, fixed = arr("fee_pct"), arr("min_fee"), arr("fixed_fee")
    tob = arr("tob_pct")
    caps = [cost_models[c].tob_cap for c in columns]
    tob_cap = np.array([np.inf if cap is None else float(cap) * cost_multiplier for cap in caps])
    half_spread = arr("spread_bps") / 10_000.0 / 2.0
    slip_k = arr("slippage_k")

    n = len(prices)
    gross = (w * rets).sum(axis=1)
    net = np.zeros(n)
    turnover = np.zeros(n)
    equity = initial_capital
    prev_w = np.zeros(len(columns))
    for t in range(n):
        dw = np.abs(w[t] - prev_w)
        turnover[t] = dw.sum()
        cost = 0.0
        if turnover[t] > 0:
            notional = dw * equity
            traded = notional > 0
            commission = np.where(traded, np.maximum(notional * fee, min_fee) + fixed, 0.0)
            tob_cost = np.minimum(notional * tob, tob_cap)
            impact = notional * (half_spread + slip_k * vol_arr[t])
            cost = float((commission + tob_cost + impact).sum()) / equity
        net[t] = gross[t] - cost
        equity *= 1.0 + net[t]
        prev_w = w[t]
        if equity <= 0:
            net[t + 1 :] = 0.0
            break

    index = prices.index
    net_s = pd.Series(net, index=index)
    return VectorizedResult(
        equity_curve=(1.0 + net_s).cumprod(),
        net_returns=net_s,
        gross_returns=pd.Series(gross, index=index),
        applied_weights=applied,
        raw_weights=target_weights,
        turnover_per_bar=pd.Series(turnover, index=index),
    )
