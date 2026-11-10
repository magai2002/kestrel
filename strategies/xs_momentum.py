"""``CrossSectionalMomentum`` (spec Section 5, item 2 — secondary,
mainly for the ETF sleeve).

Monthly rebalance. Rank the universe by trailing total return over
``lookback_months`` (6 or 12); of the top ``top_n`` by rank, drop any
with non-positive trailing return (dual momentum: being top-ranked in a
falling universe still means flat, not long) and risk-weight the
survivors by inverse realized volatility.

**Interpreting "equally risk-weighted" when fewer than ``top_n``
survive the dual-momentum filter.** The spec doesn't pin down what
happens to the "slots" that fail the absolute-momentum filter. We
treat them as cash: survivors are inverse-vol-weighted *among
themselves* (so each survivor still contributes equal risk, matching
"equally risk-weighted"), then the whole survivor allocation is scaled
by ``n_survivors / top_n`` — if all ``top_n`` candidates pass, the
sleeve is fully allocated across them; if only one does, it gets
``1/top_n`` of that allocation and the rest sits uninvested, rather
than concentrating the whole sleeve into whatever one name happened to
rank highest.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from strategies.base import StrategyContext


class XSMomentumParams(BaseModel):
    model_config = ConfigDict(frozen=True)

    lookback_months: int = 12  # 6 or 12 per spec
    top_n: int = 3
    rebalance_freq: str = "ME"  # pandas offset alias: month-end
    vol_span_days: float = 35.0
    bars_per_day: float = 1.0
    periods_per_year: float = 252.0


def required_lookback_bars(params: XSMomentumParams) -> int:
    lookback_days = params.lookback_months * 31  # calendar days, deliberately generous
    total_days = lookback_days + params.vol_span_days
    return round(total_days * params.bars_per_day * 1.2)


def _rebalance_positions(index: pd.DatetimeIndex, freq: str) -> list[int]:
    """Bar positions closest to (at or before) each period's end."""
    positions = pd.Series(np.arange(len(index)), index=index)
    last_per_period = positions.resample(freq).last().dropna()
    return [int(p) for p in last_per_period.values]


def _lookback_return(
    prices: pd.Series, at_position: int, lookback_months: int, index: pd.DatetimeIndex
) -> float:
    target_date = index[at_position] - pd.DateOffset(months=lookback_months)
    lookback_pos = index.searchsorted(target_date, side="right") - 1
    if lookback_pos < 0:
        return float("nan")
    base = prices.iloc[lookback_pos]
    if base == 0 or np.isnan(base):
        return float("nan")
    return float(prices.iloc[at_position] / base - 1.0)


def compute_weights(prices: pd.DataFrame, params: XSMomentumParams) -> pd.DataFrame:
    if prices.empty:
        return prices.copy()
    # DataFrame.index is typed generically (Index[Any]); every caller
    # in this project uses a DatetimeIndex (bar close times), so this
    # cast reflects an invariant that's true in practice, not an
    # assumption being newly introduced here.
    index = pd.DatetimeIndex(prices.index)
    vol_span = max(2, round(params.vol_span_days * params.bars_per_day))
    pct_vol = prices.pct_change().ewm(span=vol_span, min_periods=vol_span).std()

    weights = pd.DataFrame(np.nan, index=index, columns=prices.columns)

    for pos in _rebalance_positions(index, params.rebalance_freq):
        lookback_returns = pd.Series(
            {
                col: _lookback_return(prices[col], pos, params.lookback_months, index)
                for col in prices.columns
            }
        )
        ranked = lookback_returns.dropna().sort_values(ascending=False)
        candidates = ranked.index[: params.top_n]
        survivors = [c for c in candidates if lookback_returns[c] > 0]

        row = pd.Series(0.0, index=prices.columns)
        inv_vol = {}
        for c in survivors:
            vol = pct_vol[c].iloc[pos]
            if vol and not np.isnan(vol) and vol > 0:
                inv_vol[c] = 1.0 / vol
        if inv_vol:
            total_inv_vol = sum(inv_vol.values())
            scale = len(inv_vol) / params.top_n
            for c, iv in inv_vol.items():
                row[c] = (iv / total_inv_vol) * scale
        weights.iloc[pos] = row.values

    return weights.ffill().fillna(0.0)


@dataclass
class CrossSectionalMomentum:
    """``Strategy``-protocol wrapper. Rebalance-and-hold has no
    path-dependent memory beyond what's needed to find the most recent
    rebalance decision, so (as with ``TrendVolTarget``) a bounded
    trailing window is enough — unlike ``BuyAndHold``'s entry price."""

    params: XSMomentumParams = field(default_factory=XSMomentumParams)

    def on_bar(self, ctx: StrategyContext) -> dict[str, float]:
        lookback = required_lookback_bars(self.params)
        prices = {
            instrument_id: bars["close"].iloc[-lookback:]
            for instrument_id, bars in ctx.history.items()
        }
        prices_df = pd.DataFrame(prices)
        weights_df = compute_weights(prices_df, self.params)
        if weights_df.empty:
            return {instrument_id: 0.0 for instrument_id in ctx.history}
        last_row = weights_df.iloc[-1]
        return {str(k): float(v) for k, v in last_row.items()}
