"""Spec Section 11, "Golden" layer: a frozen data fixture plus config
produces a stored results hash; any change must be deliberate.

If this test fails after a change you *intended* to make (a strategy
tweak, a cost-model change, a rounding fix), that's expected — recompute
the hash (run this file's ``__main__`` block, or just read the
``AssertionError``'s "got" value) and update ``EXPECTED_HASH`` as part
of that same commit, so the diff shows a human consciously accepted a
behavior change rather than someone discovering it by accident months
later.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import numpy as np
import pandas as pd

from backtest.engine import run_backtest
from core.enums import AssetClass, Timeframe, Venue
from core.instrument import Instrument
from execution.costs import CostModel
from risk.limits import RiskLimits
from strategies.trend_vol_target import TrendVolTarget, TrendVolTargetParams

EXPECTED_HASH = "5a95858375661206"

FROZEN_PARAMS = TrendVolTargetParams(
    speeds_days=((4, 8), (6, 12)), vol_span_days=5, bars_per_day=1.0, periods_per_year=365.0
)


def _frozen_prices(n: int = 80, seed: int = 42) -> pd.DataFrame:
    """Deterministic, seeded -- must never change, or the fixture is no
    longer "frozen" and the whole point of this test is lost."""
    rng = np.random.default_rng(seed)
    log_returns = rng.normal(0.002, 0.015, n)
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
            "high": np.maximum(open_, close) * 1.002,
            "low": np.minimum(open_, close) * 0.998,
            "close": close,
            "volume": 1_000_000.0,
        }
    )


def _run_frozen_backtest():
    instrument_id = "binance:BTCUSDT"
    instrument = Instrument(
        venue=Venue.BINANCE,
        symbol="BTCUSDT",
        asset_class=AssetClass.CRYPTO,
        base_currency="BTC",
        quote_currency="USDT",
        tick_size=Decimal("0.01"),
        step_size=Decimal("0.00001"),
        min_notional=Decimal("5"),
    )
    return run_backtest(
        strategy=TrendVolTarget(params=FROZEN_PARAMS),
        strategy_name="trend_vol_target",
        sleeve_name="crypto",
        prices={instrument_id: _frozen_prices()},
        instruments={instrument_id: instrument},
        timeframe=Timeframe.D1,
        initial_capital=Decimal("10000"),
        cost_models={
            instrument_id: CostModel(
                fee_pct=Decimal("0.008"), spread_bps=Decimal("2"), slippage_k=Decimal("0.1")
            )
        },
        risk_limits=RiskLimits(per_instrument_weight_cap=1.0, max_order_notional_pct=1.0),
        periods_per_year=365,
        run_id="golden-test",
        buffer_pct=0.10,
        participation_cap=Decimal("0.05"),
        vol_span_days=FROZEN_PARAMS.vol_span_days,
        bars_per_day=FROZEN_PARAMS.bars_per_day,
    )


def _result_hash(results) -> str:
    """Hashes the parts of the result that should be perfectly
    reproducible given frozen data and config -- not git_sha (changes
    every commit) or run_id (a UUID)."""
    payload = {
        "equity_curve": [round(v, 8) for v in results.equity_curve.tolist()],
        "n_orders": results.n_orders,
        "n_fills": results.n_fills,
        "n_rejected_orders": results.n_rejected_orders,
        "kill_switch_tripped": results.kill_switch_tripped,
    }
    canonical = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def test_golden_backtest_hash_is_unchanged() -> None:
    results = _run_frozen_backtest()
    actual_hash = _result_hash(results)
    assert actual_hash == EXPECTED_HASH, (
        f"golden hash changed: expected {EXPECTED_HASH}, got {actual_hash}. "
        "If this change was deliberate, update EXPECTED_HASH in this file."
    )


if __name__ == "__main__":
    # Run directly (`uv run python tests/unit/backtest/test_golden.py`)
    # to print the current hash when deliberately updating EXPECTED_HASH.
    print(_result_hash(_run_frozen_backtest()))
