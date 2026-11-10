"""Baselines (spec Section 5, item 4): every report shows a strategy
next to its baseline for the same period and costs, so these have to
be fair comparisons, not strawmen.

**Why "buy and hold" needs care in a weights-based framework.**
Design rule 1 has every strategy emit a *target weight*, re-asserted
every bar; the order manager trades the difference from current
holdings. Taken literally, a strategy that emits the same constant
weight every bar asks the portfolio to keep rebuying/trimming to hold
that fraction flat as prices diverge — that's ``EqualWeightRebalanced``,
not "buy and hold." True buy-and-hold means: buy a fixed quantity once,
then never trade again, letting the weight *drift* with relative price
moves. ``BuyAndHold`` below emits that drifting weight explicitly, so
feeding it through the same vectorized/event-driven machinery as every
other strategy produces (near) zero turnover, as it should, without
needing a special-cased no-cost backtest path just for this one case.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
from pydantic import BaseModel, ConfigDict

from strategies.base import StrategyContext


class BuyAndHoldParams(BaseModel):
    model_config = ConfigDict(frozen=True)
    initial_weights: dict[str, float] | None = None  # None => equal-weight at entry


class EqualWeightRebalancedParams(BaseModel):
    model_config = ConfigDict(frozen=True)
    target_gross_exposure: float = 1.0  # e.g. 1.0 = fully invested, split equally


def _initial_weights(columns: list[str], params: BuyAndHoldParams) -> dict[str, float]:
    if params.initial_weights is not None:
        return params.initial_weights
    if not columns:
        return {}
    equal = 1.0 / len(columns)
    return {c: equal for c in columns}


def buy_and_hold_weights(prices: pd.DataFrame, params: BuyAndHoldParams) -> pd.DataFrame:
    """Drifting weight trajectory of buying ``initial_weights`` once at
    the first row and never trading again."""
    if prices.empty:
        return prices.copy()
    initial = _initial_weights(list(prices.columns), params)
    entry_prices = prices.iloc[0]
    held_value = pd.DataFrame(
        {c: initial.get(c, 0.0) * (prices[c] / entry_prices[c]) for c in prices.columns},
        index=prices.index,
    )
    total_value = held_value.sum(axis=1)
    # total_value is never 0 unless every initial weight is 0.
    weights = held_value.div(total_value.where(total_value != 0, 1.0), axis=0)
    return weights


def equal_weight_rebalanced_weights(
    prices: pd.DataFrame, params: EqualWeightRebalancedParams
) -> pd.DataFrame:
    """Constant 1/N target weight (times gross exposure) every bar."""
    if prices.empty or len(prices.columns) == 0:
        return prices.copy()
    per_instrument = params.target_gross_exposure / len(prices.columns)
    return pd.DataFrame(per_instrument, index=prices.index, columns=prices.columns)


@dataclass
class BuyAndHold:
    """``Strategy``-protocol wrapper. Entry prices are remembered the
    first time each instrument is seen and never recomputed from a
    truncated window — unlike the EWMA-based strategies, truncating
    history here would lose the actual entry price and silently change
    what "buy and hold" means partway through a run."""

    params: BuyAndHoldParams = field(default_factory=BuyAndHoldParams)
    _entry_prices: dict[str, float] = field(default_factory=dict)
    _initial_weights_cache: dict[str, float] | None = field(default=None, init=False)

    def on_bar(self, ctx: StrategyContext) -> dict[str, float]:
        instruments = sorted(ctx.history.keys())
        if self._initial_weights_cache is None:
            self._initial_weights_cache = _initial_weights(instruments, self.params)
        for instrument_id in instruments:
            if instrument_id not in self._entry_prices:
                self._entry_prices[instrument_id] = float(
                    ctx.history[instrument_id]["close"].iloc[0]
                )

        held_value = {
            instrument_id: self._initial_weights_cache.get(instrument_id, 0.0)
            * (
                float(ctx.history[instrument_id]["close"].iloc[-1])
                / self._entry_prices[instrument_id]
            )
            for instrument_id in instruments
        }
        total = sum(held_value.values())
        if total == 0:
            return {k: 0.0 for k in held_value}
        return {k: v / total for k, v in held_value.items()}


@dataclass
class EqualWeightRebalanced:
    params: EqualWeightRebalancedParams = field(default_factory=EqualWeightRebalancedParams)

    def on_bar(self, ctx: StrategyContext) -> dict[str, float]:
        instruments = list(ctx.history.keys())
        if not instruments:
            return {}
        per_instrument = self.params.target_gross_exposure / len(instruments)
        return {instrument_id: per_instrument for instrument_id in instruments}
