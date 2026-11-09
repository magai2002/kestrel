from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.requests import StockBarsRequest

from core.enums import Venue
from data import BAR_COLUMNS
from data.sources import alpaca_bars


@dataclass
class _FakeBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class _FakeBarSet:
    def __init__(self, data: dict[str, list[_FakeBar]]) -> None:
        self.data = data


class _FakeClient:
    def __init__(self, data: dict[str, list[_FakeBar]]) -> None:
        self._data = data
        self.last_request: StockBarsRequest | None = None

    def get_stock_bars(self, request_params: StockBarsRequest) -> _FakeBarSet:
        self.last_request = request_params
        return _FakeBarSet(self._data)


def test_uses_delayed_sip_feed_and_full_adjustment() -> None:
    """The free-plan-correct feed is `delayed_sip`, not `sip` (sip is a
    paid feed per alpaca-py's own DataFeed docstring) -- see the module
    docstring for the source citation."""
    client = _FakeClient({"SPY": []})
    alpaca_bars.fetch_daily_bars(["SPY"], date(2024, 1, 1), date(2024, 1, 31), client)
    assert client.last_request.feed == DataFeed.DELAYED_SIP
    assert client.last_request.adjustment == Adjustment.ALL


def test_parses_bars_into_unified_schema() -> None:
    bars = [
        _FakeBar(
            timestamp=datetime(2024, 1, 2, tzinfo=UTC),
            open=470,
            high=472,
            low=469,
            close=471,
            volume=1000,
        ),
        _FakeBar(
            timestamp=datetime(2024, 1, 3, tzinfo=UTC),
            open=471,
            high=473,
            low=470,
            close=472,
            volume=1100,
        ),
    ]
    client = _FakeClient({"SPY": bars})
    df = alpaca_bars.fetch_daily_bars(["SPY"], date(2024, 1, 1), date(2024, 1, 31), client)

    assert list(df.columns) == list(BAR_COLUMNS)
    assert len(df) == 2
    assert set(df["venue"]) == {Venue.ALPACA.value}
    assert set(df["symbol"]) == {"SPY"}
    assert set(df["timeframe"]) == {"1d"}
    assert (df["close_time"] - df["open_time"] == pd.Timedelta(days=1)).all()


def test_multiple_symbols_produce_one_combined_frame() -> None:
    client = _FakeClient(
        {
            "SPY": [_FakeBar(datetime(2024, 1, 2, tzinfo=UTC), 470, 472, 469, 471, 1000)],
            "QQQ": [_FakeBar(datetime(2024, 1, 2, tzinfo=UTC), 400, 402, 399, 401, 900)],
        }
    )
    df = alpaca_bars.fetch_daily_bars(["SPY", "QQQ"], date(2024, 1, 1), date(2024, 1, 31), client)
    assert set(df["symbol"]) == {"SPY", "QQQ"}


def test_symbol_with_no_bars_is_simply_absent() -> None:
    client = _FakeClient(
        {"SPY": [], "QQQ": [_FakeBar(datetime(2024, 1, 2, tzinfo=UTC), 1, 1, 1, 1, 1)]}
    )
    df = alpaca_bars.fetch_daily_bars(["SPY", "QQQ"], date(2024, 1, 1), date(2024, 1, 31), client)
    assert set(df["symbol"]) == {"QQQ"}


def test_no_data_at_all_returns_empty_frame_with_correct_columns() -> None:
    client = _FakeClient({"SPY": []})
    df = alpaca_bars.fetch_daily_bars(["SPY"], date(2024, 1, 1), date(2024, 1, 31), client)
    assert df.empty
    assert list(df.columns) == list(BAR_COLUMNS)
