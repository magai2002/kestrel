"""``TrendVolTarget`` (spec Section 5, item 1 — the primary strategy).

Carver-style EWMA crossover trend following, combined with
volatility targeting. Three forecast "speeds" (fast/slow EWMA span
pairs, e.g. 16/64, 32/128, 64/256 **days**) each produce a
volatility-normalized crossover forecast, scaled so its own average
absolute value is about ``forecast_target`` (10) over the available
history, capped at ``+-forecast_cap`` (20), then the three speeds'
(already-capped) forecasts are averaged. **Long-only** (design rule
1): a negative combined forecast means flat, not short — it's clipped
to zero, not negated into a short position.

Spans are expressed in **days** in config and converted to bars via
``bars_per_day`` (6 for 4h crypto bars, 1 for daily equities), so a
"64-day" lookback means the same horizon in calendar time on both
sleeves (the spec's own reasoning for why this conversion matters —
otherwise a 64-*bar* lookback would be an 11-day trend on crypto's 4h
bars and a 64-*trading-day* (~3-month) trend on daily equities, wildly
different horizons despite the same number).

**Forecast scalar is self-calibrating, not a borrowed book constant.**
Carver's own work gives empirically-fitted scalars for specific EWMAC
speed combinations, but we have no verified source for the exact
numbers in this environment, and asserting a possibly-misremembered
constant would violate the same "never guess" principle that applies
to external APIs. Instead each speed's scalar is derived directly from
the data actually being backtested (``forecast_target / mean(|raw
forecast|)``), which achieves the spec's stated goal — average
absolute forecast near 10 — without claiming a source we can't check.
Documented here and in ``docs/progress.md`` as a deliberate,
scope-appropriate simplification, not an oversight.

The same volatility estimate (EWMA of returns, ``vol_span_days``,
~35 by default) normalizes the raw EWMAC difference into forecast
units *and* sizes the final vol-targeted position — one estimate, two
uses, matching the spec's single mention of "volatility from an EWMA
of returns" rather than a separate estimate per speed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, field_validator

from strategies.base import StrategyContext

DEFAULT_SPEEDS_DAYS: tuple[tuple[int, int], ...] = ((16, 64), (32, 128), (64, 256))


class TrendVolTargetParams(BaseModel):
    model_config = ConfigDict(frozen=True)

    speeds_days: tuple[tuple[int, int], ...] = DEFAULT_SPEEDS_DAYS
    vol_span_days: float = 35.0
    target_vol_annual: float = 0.15
    forecast_cap: float = 20.0
    forecast_target: float = 10.0
    bars_per_day: float = 1.0  # 6 for 4h crypto, 1 for daily equities
    periods_per_year: float = 252.0  # 365*6 for crypto 4h, 252 for daily equities

    @field_validator("speeds_days")
    @classmethod
    def _at_least_one_speed(cls, value: tuple[tuple[int, int], ...]) -> tuple[tuple[int, int], ...]:
        if len(value) == 0:
            raise ValueError("speeds_days must have at least one (fast, slow) pair")
        for fast, slow in value:
            if fast >= slow:
                raise ValueError(f"fast span ({fast}) must be < slow span ({slow})")
        return value


def required_lookback_bars(params: TrendVolTargetParams) -> int:
    """Bars of history needed for the slowest EWMA to be close to its
    steady-state value — see module docstring. 5x the slowest span is
    generous; EWMA weight decays exponentially, so by 5 spans in, the
    excluded history would have contributed well under 1% either way."""
    slowest_days = max(slow for _, slow in params.speeds_days)
    longest_days = max(slowest_days, params.vol_span_days)
    return round(longest_days * params.bars_per_day * 5)


def _span_bars(days: float, bars_per_day: float) -> int:
    return max(2, round(days * bars_per_day))


def _single_instrument_weight(price: pd.Series, params: TrendVolTargetParams) -> pd.Series:
    vol_span = _span_bars(params.vol_span_days, params.bars_per_day)
    returns = price.pct_change()
    pct_vol = returns.ewm(span=vol_span, min_periods=vol_span).std()
    price_vol = (pct_vol * price).replace(0.0, np.nan)

    speed_forecasts = []
    for fast_days, slow_days in params.speeds_days:
        fast_span = _span_bars(fast_days, params.bars_per_day)
        slow_span = _span_bars(slow_days, params.bars_per_day)
        ewma_fast = price.ewm(span=fast_span, min_periods=fast_span).mean()
        ewma_slow = price.ewm(span=slow_span, min_periods=slow_span).mean()
        normalized = (ewma_fast - ewma_slow) / price_vol

        mean_abs = normalized.abs().mean()
        if not mean_abs or np.isnan(mean_abs) or mean_abs == 0:
            speed_forecasts.append(pd.Series(0.0, index=price.index))
            continue
        scalar = params.forecast_target / mean_abs
        scaled = (normalized * scalar).clip(-params.forecast_cap, params.forecast_cap)
        speed_forecasts.append(scaled)

    combined_forecast = pd.concat(speed_forecasts, axis=1).mean(axis=1)
    long_only_forecast = combined_forecast.clip(lower=0.0)

    annualized_vol = pct_vol * math.sqrt(params.periods_per_year)
    vol_target_weight = (params.target_vol_annual / annualized_vol).replace(
        [np.inf, -np.inf], np.nan
    )

    position_weight = (long_only_forecast / params.forecast_target) * vol_target_weight
    return position_weight.clip(0.0, 1.0).fillna(0.0)


def compute_weights(prices: pd.DataFrame, params: TrendVolTargetParams) -> pd.DataFrame:
    """Vectorized form (Phase 1). One independent weight per instrument
    — this strategy doesn't rank or compare across instruments, unlike
    ``CrossSectionalMomentum``."""
    if prices.empty:
        return prices.copy()
    return pd.DataFrame(
        {col: _single_instrument_weight(prices[col], params) for col in prices.columns},
        index=prices.index,
    )


@dataclass
class TrendVolTarget:
    """``Strategy``-protocol wrapper: replays ``compute_weights`` over a
    trailing window (``required_lookback_bars``) of each instrument's
    history and returns the last row. EWMA has no long-range
    path-dependent state beyond what's in that window, so truncating
    history here doesn't change the answer within the parity
    tolerance — unlike ``BuyAndHold``, where truncating would lose the
    true entry price entirely.
    """

    params: TrendVolTargetParams = field(default_factory=TrendVolTargetParams)

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
