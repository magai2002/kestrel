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

That calibration mean is a **rolling** window (``required_lookback_bars``
wide), not a whole-series average — caught by the no-look-ahead
property test (``tests/property/test_no_look_ahead.py``): a scalar fit
on the *entire* series, future bars included, would leak information
about bars that haven't happened yet into every earlier forecast. A
single bad backtesting habit, exactly the kind property-based testing
exists to catch rather than hand-picked examples.

The same volatility estimate (EWMA of returns, ``vol_span_days``,
~35 by default) normalizes the raw EWMAC difference into forecast
units *and* sizes the final vol-targeted position — one estimate, two
uses, matching the spec's single mention of "volatility from an EWMA
of returns" rather than a separate estimate per speed.

**Instrument weights (Phase 3 fix).** The sleeve has *one* vol target,
not one per instrument. Each instrument's vol-targeted position is
multiplied by its instrument weight (equal, ``1/N``, by default) and an
instrument diversification multiplier ``idm`` (default 1.0 — the
conservative choice; Carver's IDM corrects for diversification making
the combined sleeve less volatile than its parts, but we don't have a
verified estimate and a too-high one levers the sleeve up). Phases 1-2
targeted the full 15% on *every* instrument, which on the 10-ETF sleeve
asks for several hundred percent gross exposure. The single-instrument
case (and so the golden and parity tests) is unchanged: ``1/1 = 1``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, field_validator

from portfolio.sizing import vol_target_weight
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
    instrument_weight: float | None = None  # None = 1/N across the columns given
    idm: float = 1.0

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


def _single_instrument_weight(
    price: pd.Series, params: TrendVolTargetParams, instrument_weight: float = 1.0
) -> pd.Series:
    weight = _components(price, params, instrument_weight)["weight"]
    return pd.Series(weight, index=price.index)


def _components(
    price: pd.Series, params: TrendVolTargetParams, instrument_weight: float
) -> dict[str, np.ndarray]:
    """Every intermediate of the weight calculation, as arrays aligned
    with ``price``. ``weight`` is the answer; the rest exists so
    ``explain`` (the dashboard's decision trace) reports the *same*
    numbers the weight was built from, not a re-derivation that could drift.
    """
    # pandas only for the windowed kernels (ewm, rolling); every
    # elementwise step is numpy. Same arithmetic, same order, so the
    # results are bit-identical (the golden hash didn't move), but pandas'
    # per-call overhead on clip/where/replace was ~2/3 of an event
    # backtest's runtime (Phase 6 profile: 4m15s for the equities sleeve).
    out: dict[str, np.ndarray] = {}
    vol_span = _span_bars(params.vol_span_days, params.bars_per_day)
    returns = price.pct_change()
    pct_vol_s = returns.ewm(span=vol_span, min_periods=vol_span).std()
    pct_vol = pct_vol_s.to_numpy()
    price_arr = price.to_numpy(dtype=float)
    price_vol = pct_vol * price_arr
    price_vol = np.where(price_vol == 0.0, np.nan, price_vol)
    scalar_window = required_lookback_bars(params)

    speed_forecasts = []
    for k, (fast_days, slow_days) in enumerate(params.speeds_days):
        fast_span = _span_bars(fast_days, params.bars_per_day)
        slow_span = _span_bars(slow_days, params.bars_per_day)
        ewma_fast = price.ewm(span=fast_span, min_periods=fast_span).mean().to_numpy()
        ewma_slow = price.ewm(span=slow_span, min_periods=slow_span).mean().to_numpy()
        normalized = (ewma_fast - ewma_slow) / price_vol

        # Rolling, not a whole-series .mean(): a single scalar fit on
        # the full sample (including bars after the one being scored)
        # is a real look-ahead leak -- caught by the no-look-ahead
        # property test (tests/property/test_no_look_ahead.py), which
        # is exactly what that test exists to catch. A trailing window
        # the same size as required_lookback_bars keeps this exactly
        # consistent with the Strategy-protocol wrapper's windowed
        # replay below, instead of merely approximately so.
        mean_abs = (
            pd.Series(np.abs(normalized))
            .rolling(scalar_window, min_periods=vol_span)
            .mean()
            .to_numpy()
        )
        with np.errstate(divide="ignore", invalid="ignore"):
            scalar = params.forecast_target / np.where(mean_abs == 0.0, np.nan, mean_abs)
            scaled = np.clip(normalized * scalar, -params.forecast_cap, params.forecast_cap)
        speed_forecasts.append(np.where(np.isnan(scaled), 0.0, scaled))
        out[f"crossover_{k}"] = normalized
        out[f"scalar_{k}"] = scalar
        out[f"forecast_{k}"] = speed_forecasts[-1]

    combined_forecast = pd.DataFrame(np.column_stack(speed_forecasts)).mean(axis=1).to_numpy()
    long_only_forecast = np.maximum(combined_forecast, 0.0)

    annualized_vol = pct_vol * math.sqrt(params.periods_per_year)
    # Shared with the event-driven engine's per-bar sizing --
    # portfolio.sizing.vol_target_weight, not a duplicated formula.
    sized_weight = vol_target_weight(params.target_vol_annual, annualized_vol)

    position_weight = (
        (long_only_forecast / params.forecast_target)
        * sized_weight
        * instrument_weight
        * params.idm
    )
    clipped = np.clip(position_weight, 0.0, 1.0)
    out.update(
        vol_annual=annualized_vol,
        forecast=combined_forecast,
        forecast_long_only=long_only_forecast,
        vol_target_weight=sized_weight,
        weight=np.where(np.isnan(clipped), 0.0, clipped),
    )
    return out


def _num(x: float) -> float | None:
    """JSON-safe float: NaN/inf -> None."""
    x = float(x)
    return x if math.isfinite(x) else None


def explain_last(prices: pd.DataFrame, params: TrendVolTargetParams) -> dict[str, object]:
    """The decision trace for the newest row of ``prices``: per asset,
    every step from price to target weight. Only unitless or per-unit
    numbers (forecasts, volatilities, weights, prices), so it's safe
    for the public dashboard."""
    iw = params.instrument_weight
    if iw is None:
        iw = 1.0 / max(1, len(prices.columns))
    assets: dict[str, object] = {}
    weights: dict[str, float] = {}
    for col in prices.columns:
        c = _components(prices[col], params, iw)
        weights[str(col)] = float(c["weight"][-1])
        assets[str(col)] = {
            "price": _num(prices[col].iloc[-1]),
            "vol_annual": _num(c["vol_annual"][-1]),
            "speeds": [
                {
                    "fast_days": fast,
                    "slow_days": slow,
                    "crossover": _num(c[f"crossover_{k}"][-1]),
                    "scalar": _num(c[f"scalar_{k}"][-1]),
                    "forecast": _num(c[f"forecast_{k}"][-1]),
                }
                for k, (fast, slow) in enumerate(params.speeds_days)
            ],
            "forecast": _num(c["forecast"][-1]),
            "forecast_long_only": _num(c["forecast_long_only"][-1]),
            "vol_target_weight": _num(c["vol_target_weight"][-1]),
            "instrument_weight": iw,
            "idm": params.idm,
            "weight": weights[str(col)],
        }
    return {
        "kind": "trend_vol_target",
        "params": {
            "target_vol_annual": params.target_vol_annual,
            "forecast_target": params.forecast_target,
            "forecast_cap": params.forecast_cap,
            "vol_span_days": params.vol_span_days,
            "speeds_days": [list(s) for s in params.speeds_days],
        },
        "weights": weights,
        "assets": assets,
    }


def compute_weights(prices: pd.DataFrame, params: TrendVolTargetParams) -> pd.DataFrame:
    """Vectorized form (Phase 1). One independent weight per instrument
    — this strategy doesn't rank or compare across instruments, unlike
    ``CrossSectionalMomentum``."""
    if prices.empty:
        return prices.copy()
    iw = params.instrument_weight
    if iw is None:
        iw = 1.0 / len(prices.columns)
    return pd.DataFrame(
        {col: _single_instrument_weight(prices[col], params, iw) for col in prices.columns},
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

    def _window(self, ctx: StrategyContext) -> pd.DataFrame:
        lookback = required_lookback_bars(self.params)
        return pd.DataFrame(
            {
                instrument_id: bars["close"].iloc[-lookback:]
                for instrument_id, bars in ctx.history.items()
            }
        )

    def explain(self, ctx: StrategyContext) -> dict[str, object]:
        """Same window as ``on_bar``, so ``explain(ctx)["weights"] == on_bar(ctx)``."""
        prices_df = self._window(ctx)
        if prices_df.empty:
            return {"kind": "trend_vol_target", "weights": self.on_bar(ctx), "assets": {}}
        return explain_last(prices_df, self.params)

    def on_bar(self, ctx: StrategyContext) -> dict[str, float]:
        prices_df = self._window(ctx)
        weights_df = compute_weights(prices_df, self.params)
        if weights_df.empty:
            return {instrument_id: 0.0 for instrument_id in ctx.history}
        last_row = weights_df.iloc[-1]
        return {str(k): float(v) for k, v in last_row.items()}
