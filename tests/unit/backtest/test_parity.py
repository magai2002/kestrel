"""Spec Section 11, "Parity" layer: event engine vs. the vectorized
backtester on the same strategy and data, within a stated tolerance.

Configured to isolate *decision* parity specifically, not every
real-world mechanic the event engine adds on top: ``buffer_pct=0``
(removes the one genuine behavioral difference between the two paths —
the vectorized backtester buffers against a remembered previous
target, the event engine buffers against the weight actually implied
by current holdings; with buffering off, both always trade fully to
target, so that difference can't show up here), zero costs, a fine
step size (negligible rounding) and full participation (no fill
capping). What's left to differ is only: the EWMA replay window inside
``on_bar`` (already covered more precisely by each strategy's own
``test_on_bar_matches_vectorized_replay``) and Decimal rounding to
step size. A 1% final-equity tolerance comfortably covers both without
being so loose it would pass even if the two paths disagreed.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from backtest.engine import run_backtest
from core.enums import AssetClass, Timeframe, Venue
from core.instrument import Instrument
from execution.costs import CostModel
from research import vectorized
from risk.limits import RiskLimits
from strategies.trend_vol_target import TrendVolTarget, TrendVolTargetParams
from strategies.trend_vol_target import compute_weights as trend_weights

SHORT_PARAMS = TrendVolTargetParams(
    speeds_days=((2, 4), (3, 6)), vol_span_days=3, bars_per_day=1.0, periods_per_year=365.0
)
ZERO_COST = CostModel(fee_pct=Decimal("0"), spread_bps=Decimal("0"), slippage_k=Decimal("0"))


def _trending_prices(n: int, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    log_returns = 0.01 + rng.normal(0, 0.01, n)
    close = 100.0 * np.exp(np.cumsum(log_returns))
    open_time = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    open_ = np.concatenate([[100.0], close[:-1]])
    return pd.DataFrame(
        {
            "venue": "binance",
            "symbol": "BTCUSDT",
            "timeframe": "1d",
            "open_time": open_time,
            "close_time": open_time + pd.Timedelta(days=1),
            "open": open_,
            "high": np.maximum(open_, close) * 1.0001,
            "low": np.minimum(open_, close) * 0.9999,
            "close": close,
            "volume": 1_000_000.0,
        }
    )


def test_event_engine_matches_vectorized_backtest_within_tolerance() -> None:
    n = 60
    df = _trending_prices(n)
    instrument_id = "binance:BTCUSDT"

    # Vectorized reference.
    close_prices = pd.DataFrame({instrument_id: df["close"]})
    vec_weights = trend_weights(close_prices, SHORT_PARAMS)
    vec_result = vectorized.run(close_prices, vec_weights, buffer_pct=0.0, cost_bps=0.0)
    vec_final_equity = float(vec_result.equity_curve.iloc[-1])

    # Event-driven engine, same strategy, same data, equivalent (zero) costs.
    instrument = Instrument(
        venue=Venue.BINANCE,
        symbol="BTCUSDT",
        asset_class=AssetClass.CRYPTO,
        base_currency="BTC",
        quote_currency="USDT",
        tick_size=Decimal("0.0001"),
        step_size=Decimal("0.00000001"),  # fine enough that rounding is negligible
        min_notional=Decimal("0"),
    )
    engine_results = run_backtest(
        strategy=TrendVolTarget(params=SHORT_PARAMS),
        strategy_name="trend_vol_target",
        sleeve_name="crypto",
        prices={instrument_id: df},
        instruments={instrument_id: instrument},
        timeframe=Timeframe.D1,
        initial_capital=Decimal("1"),  # same $1 base as the vectorized equity curve
        cost_models={instrument_id: ZERO_COST},
        risk_limits=RiskLimits(per_instrument_weight_cap=1.0, max_order_notional_pct=1.0),
        periods_per_year=365,
        run_id="parity-test",
        buffer_pct=0.0,
        participation_cap=Decimal("1.0"),
        vol_span_days=SHORT_PARAMS.vol_span_days,
        bars_per_day=SHORT_PARAMS.bars_per_day,
    )
    engine_final_equity = float(engine_results.equity_curve.iloc[-1])

    assert engine_final_equity == pytest.approx(vec_final_equity, rel=0.01)
