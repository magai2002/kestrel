"""Engine-level tests: a simple, hand-verifiable scenario
(EqualWeightRebalanced on one instrument, zero costs, a known price
path) exercises the full decide -> risk -> fill -> ledger loop end to
end without needing a real strategy's internal complexity to reason
about.
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
from risk.limits import RiskLimits
from strategies.baselines import EqualWeightRebalanced, EqualWeightRebalancedParams


def _prices(n: int, start_price: float = 100.0, daily_return: float = 0.0) -> pd.DataFrame:
    open_time = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    close = start_price * (1.0 + daily_return) ** np.arange(1, n + 1)
    open_ = np.concatenate([[start_price], close[:-1]])
    return pd.DataFrame(
        {
            "venue": "binance",
            "symbol": "BTCUSDT",
            "timeframe": "1d",
            "open_time": open_time,
            "close_time": open_time + pd.Timedelta(days=1),
            "open": open_,
            "high": np.maximum(open_, close) * 1.001,
            "low": np.minimum(open_, close) * 0.999,
            "close": close,
            "volume": 1_000_000.0,
        }
    )


def _instrument() -> Instrument:
    return Instrument(
        venue=Venue.BINANCE,
        symbol="BTCUSDT",
        asset_class=AssetClass.CRYPTO,
        base_currency="BTC",
        quote_currency="USDT",
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.0001"),
        min_notional=Decimal("1"),
    )


def _run(n: int = 30, daily_return: float = 0.0, cost_model: CostModel | None = None):
    prices = {"binance:BTCUSDT": _prices(n, daily_return=daily_return)}
    instruments = {"binance:BTCUSDT": _instrument()}
    strategy = EqualWeightRebalanced(EqualWeightRebalancedParams(target_gross_exposure=1.0))
    return run_backtest(
        strategy=strategy,
        strategy_name="equal_weight_rebalanced",
        sleeve_name="crypto",
        prices=prices,
        instruments=instruments,
        timeframe=Timeframe.D1,
        initial_capital=Decimal("10000"),
        cost_models={
            "binance:BTCUSDT": cost_model
            or CostModel(fee_pct=Decimal("0"), spread_bps=Decimal("0"), slippage_k=Decimal("0"))
        },
        risk_limits=RiskLimits(per_instrument_weight_cap=1.0, max_order_notional_pct=1.0),
        periods_per_year=365,
        run_id="test-run",
        buffer_pct=0.0,
        participation_cap=Decimal("1.0"),
    )


def test_runs_without_error_and_produces_full_length_equity_curve() -> None:
    results = _run(n=30)
    assert results.n_bars == 30
    assert len(results.equity_curve) == 30


def test_fully_invested_zero_cost_equity_roughly_tracks_price() -> None:
    results = _run(n=30, daily_return=0.01)
    # Fully invested at ~100% weight with zero costs: equity growth
    # should be close to the asset's own growth over the same period
    # (not exact, since the first bar or two is getting invested).
    price_growth = 1.01**30
    equity_growth = float(results.equity_curve.iloc[-1] / 10000.0)
    assert equity_growth == pytest.approx(price_growth, rel=0.02)


def test_places_and_fills_at_least_one_order() -> None:
    results = _run(n=30)
    assert results.n_orders >= 1
    assert results.n_fills >= 1
    assert results.n_rejected_orders == 0


def test_no_kill_switch_trip_under_normal_conditions() -> None:
    results = _run(n=30, daily_return=0.001)
    assert not results.kill_switch_tripped


def test_reproducibility_metadata_present() -> None:
    results = _run(n=30)
    assert results.git_sha is not None
    assert len(results.config_hash) == 16
    assert len(results.data_hash) == 16


def test_rejects_misaligned_instrument_bars() -> None:
    prices = {
        "binance:BTCUSDT": _prices(30),
        "binance:ETHUSDT": _prices(29),  # different length -> misaligned
    }
    instruments = {"binance:BTCUSDT": _instrument(), "binance:ETHUSDT": _instrument()}
    with pytest.raises(ValueError, match="aligned"):
        run_backtest(
            strategy=EqualWeightRebalanced(),
            strategy_name="x",
            sleeve_name="crypto",
            prices=prices,
            instruments=instruments,
            timeframe=Timeframe.D1,
            initial_capital=Decimal("10000"),
            cost_models={"binance:BTCUSDT": CostModel(), "binance:ETHUSDT": CostModel()},
            risk_limits=RiskLimits(),
            periods_per_year=365,
            run_id="x",
        )


def test_kill_switch_trips_on_crash_and_halts_new_positions() -> None:
    # A sharp, sustained crash should trip the drawdown kill switch.
    results = _run(
        n=40,
        daily_return=-0.05,
        cost_model=CostModel(
            fee_pct=Decimal("0"), spread_bps=Decimal("0"), slippage_k=Decimal("0")
        ),
    )
    assert results.kill_switch_tripped
    assert results.kill_switch_reason is not None
