"""Maps a strategy name (as used on the CLI, e.g. ``kestrel backtest
trend_vol_target --sleeve crypto``) to its params schema, vectorized
function and ``Strategy``-protocol class."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd
from pydantic import BaseModel

from strategies import baselines, mean_reversion, trend_vol_target, xs_momentum


@dataclass(frozen=True)
class StrategyEntry:
    name: str
    params_cls: type[BaseModel]
    # Each entry's compute_weights actually takes that entry's own
    # specific Params subtype, not BaseModel in general -- Callable's
    # parameter types are contravariant, so a heterogeneous registry of
    # them can't be typed more precisely than `Any` here without each
    # entry needing its own generic StrategyEntry[ParamsT] (not worth it
    # for five fixed entries).
    compute_weights: Callable[[pd.DataFrame, Any], pd.DataFrame]
    strategy_cls: type


REGISTRY: dict[str, StrategyEntry] = {
    "trend_vol_target": StrategyEntry(
        name="trend_vol_target",
        params_cls=trend_vol_target.TrendVolTargetParams,
        compute_weights=trend_vol_target.compute_weights,
        strategy_cls=trend_vol_target.TrendVolTarget,
    ),
    "xs_momentum": StrategyEntry(
        name="xs_momentum",
        params_cls=xs_momentum.XSMomentumParams,
        compute_weights=xs_momentum.compute_weights,
        strategy_cls=xs_momentum.CrossSectionalMomentum,
    ),
    "mean_reversion": StrategyEntry(
        name="mean_reversion",
        params_cls=mean_reversion.MeanReversionParams,
        compute_weights=mean_reversion.compute_weights,
        strategy_cls=mean_reversion.ShortTermMeanReversion,
    ),
    "buy_and_hold": StrategyEntry(
        name="buy_and_hold",
        params_cls=baselines.BuyAndHoldParams,
        compute_weights=baselines.buy_and_hold_weights,
        strategy_cls=baselines.BuyAndHold,
    ),
    "equal_weight_rebalanced": StrategyEntry(
        name="equal_weight_rebalanced",
        params_cls=baselines.EqualWeightRebalancedParams,
        compute_weights=baselines.equal_weight_rebalanced_weights,
        strategy_cls=baselines.EqualWeightRebalanced,
    ),
}


def get(name: str) -> StrategyEntry:
    try:
        return REGISTRY[name]
    except KeyError:
        available = ", ".join(sorted(REGISTRY))
        raise KeyError(f"unknown strategy {name!r}; available: {available}") from None
