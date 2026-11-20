"""Shared fixtures for live-runner tests: a tiny 2-asset 24/7 sleeve,
synthetic daily bars, a feed that serves them up to ``now``."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from config.sleeve import Listing, SleeveConfig
from core.enums import AssetClass, Timeframe
from core.instrument import Instrument
from execution.costs import CostModel
from live.sleeve_runner import SleeveRuntime
from live.state import InMemoryStateStore
from observability.alerts import RecordingAlerter
from strategies.trend_vol_target import TrendVolTarget, TrendVolTargetParams

N = 120
START = pd.Timestamp("2026-01-01", tz="UTC")
PARAMS = TrendVolTargetParams(
    speeds_days=((3, 9), (6, 18)), vol_span_days=5, bars_per_day=1, periods_per_year=365
)


def sleeve(venue: str = "binance_testnet", **risk: Any) -> SleeveConfig:
    def listing(sym: str) -> dict[str, str]:
        return {"venue": venue, "symbol": sym, "base_currency": sym[:3], "quote_currency": "USDT"}

    return SleeveConfig.model_validate(
        {
            "name": "crypto",
            "asset_class": "crypto",
            "decision_bar": "1d",
            "base_currency": "USD",
            "paper_capital": 10_000,
            "periods_per_year_days": 365,
            "regime_benchmark": "BTC",
            "bar_close_delay_seconds": 30,
            "assets": [
                {
                    "id": "BTC",
                    "research": listing("BTCUSDT"),
                    "live": listing("BTCUSDT"),
                    "costs": {"fee_pct": "0.001", "spread_bps": "2", "slippage_k": "0.1"},
                },
                {
                    "id": "ETH",
                    "research": listing("ETHUSDT"),
                    "live": listing("ETHUSDT"),
                    "costs": {"fee_pct": "0.001", "spread_bps": "3", "slippage_k": "0.1"},
                },
            ],
            "risk": {
                "per_instrument_weight_cap": 0.6,
                "daily_loss_limit_pct": 0.5,
                "drawdown_limit_pct": 0.9,
                **risk,
            },
            "strategies": [{"name": "trend_vol_target", "weight": 1.0}],
        }
    )


def bars(venue: str = "binance_testnet", seed: int = 3, n: int = N) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    out = {}
    open_time = pd.date_range(START, periods=n, freq="1D")
    for j, sym in enumerate(("BTCUSDT", "ETHUSDT")):
        r = 0.004 + rng.normal(0, 0.02, n) * (1 + j * 0.3)
        close = (40_000 if j == 0 else 2_500) * np.exp(np.cumsum(r))
        open_ = np.concatenate([[close[0]], close[:-1]]) * (1 + rng.normal(0, 0.002, n))
        out[sym] = pd.DataFrame(
            {
                "venue": venue,
                "symbol": sym,
                "timeframe": "1d",
                "open_time": open_time,
                "close_time": open_time + pd.Timedelta(days=1),
                "open": open_,
                "high": np.maximum(open_, close) * 1.01,
                "low": np.minimum(open_, close) * 0.99,
                "close": close,
                "volume": 1e6,
            }
        )
    return out


class FakeFeed:
    name = "fake"

    def __init__(self, data: dict[str, pd.DataFrame]) -> None:
        self.data = data
        self.fail = False

    def fetch(self, listing: Listing, timeframe: Timeframe, now: datetime) -> pd.DataFrame:
        if self.fail:
            raise ConnectionError("feed down")
        df = self.data[listing.symbol]
        return df[df["close_time"] <= pd.Timestamp(now)].tail(10).reset_index(drop=True)


def instruments(venue: str = "binance_testnet") -> dict[str, Instrument]:
    from core.enums import Venue

    return {
        aid: Instrument(
            venue=Venue(venue),
            symbol=f"{aid}USDT",
            asset_class=AssetClass.CRYPTO,
            base_currency=aid,
            quote_currency="USDT",
            tick_size=Decimal("0.01"),
            step_size=Decimal("0.00001"),
            min_notional=Decimal("5"),
        )
        for aid in ("BTC", "ETH")
    }


def runtime(
    tmp: Path,
    data: dict[str, pd.DataFrame],
    mode: str = "sim",
    broker: Any = None,
    store: InMemoryStateStore | None = None,
    **risk: Any,
) -> SleeveRuntime:
    sl = sleeve(**risk)
    return SleeveRuntime(
        mode=mode,
        sleeve=sl,
        strategy=TrendVolTarget(params=PARAMS),
        strategy_label="trend_vol_target",
        feed=FakeFeed(data),
        store=store or InMemoryStateStore(),
        alerter=RecordingAlerter(),
        data_dir=tmp,
        instruments=instruments(),
        cost_models={a.id: a.costs.to_cost_model() for a in sl.assets},
        run_id="test",
        broker=broker,
        quote_currency="USDT",
    )


def bar_close(i: int) -> datetime:
    return (START + pd.Timedelta(days=i + 1)).to_pydatetime()


def at(i: int, delay: int = 60) -> datetime:
    return bar_close(i) + timedelta(seconds=delay)


def cost_models() -> dict[str, CostModel]:
    return {a.id: a.costs.to_cost_model() for a in sleeve().assets}
