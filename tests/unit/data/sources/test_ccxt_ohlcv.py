from __future__ import annotations

import pandas as pd
import pytest

from core.enums import Timeframe, Venue
from data.sources import ccxt_ohlcv


class _FakeKraken:
    """Duck-types just enough of ccxt.Exchange for this module."""

    id = "kraken"

    def __init__(self, candles: list[list[float]], supports_ohlcv: bool = True) -> None:
        self._candles = candles
        self.has = {"fetchOHLCV": supports_ohlcv}
        self.last_call: dict[str, object] | None = None

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        self.last_call = {"symbol": symbol, "timeframe": timeframe, "limit": limit}
        return self._candles


def _candle(open_ms: int, o: float, h: float, low: float, c: float, v: float) -> list[float]:
    return [open_ms, o, h, low, c, v]


def test_drops_the_final_still_forming_candle() -> None:
    candles = [
        _candle(1_700_000_000_000, 100, 101, 99, 100.5, 10),
        _candle(1_700_014_400_000, 100.5, 102, 100, 101, 12),  # this one should be dropped
    ]
    exchange = _FakeKraken(candles)
    df = ccxt_ohlcv.fetch_recent_ohlcv("BTC-USD", Timeframe.H4, exchange=exchange)
    assert len(df) == 1
    assert df.iloc[0]["close"] == 100.5


def test_converts_dash_symbol_to_ccxt_slash_symbol() -> None:
    exchange = _FakeKraken(
        [_candle(1_700_000_000_000, 1, 1, 1, 1, 1), _candle(1_700_014_400_000, 1, 1, 1, 1, 1)]
    )
    ccxt_ohlcv.fetch_recent_ohlcv("BTC-USD", Timeframe.H4, exchange=exchange)
    assert exchange.last_call["symbol"] == "BTC/USD"


def test_output_schema_and_venue_label() -> None:
    exchange = _FakeKraken(
        [
            _candle(1_700_000_000_000, 100, 101, 99, 100.5, 10),
            _candle(1_700_014_400_000, 0, 0, 0, 0, 0),
        ]
    )
    df = ccxt_ohlcv.fetch_recent_ohlcv("BTC-USD", Timeframe.H4, exchange=exchange)
    assert df.iloc[0]["venue"] == Venue.KRAKEN.value
    assert df.iloc[0]["symbol"] == "BTC-USD"  # canonical dash spelling preserved in storage
    assert df.iloc[0]["close_time"] - df.iloc[0]["open_time"] == pd.Timedelta(hours=4)


def test_empty_response_returns_empty_frame() -> None:
    exchange = _FakeKraken([])
    df = ccxt_ohlcv.fetch_recent_ohlcv("BTC-USD", Timeframe.H4, exchange=exchange)
    assert df.empty


def test_only_one_candle_total_returns_empty_after_dropping_unclosed() -> None:
    exchange = _FakeKraken([_candle(1_700_000_000_000, 1, 1, 1, 1, 1)])
    df = ccxt_ohlcv.fetch_recent_ohlcv("BTC-USD", Timeframe.H4, exchange=exchange)
    assert df.empty


def test_raises_if_exchange_does_not_support_fetch_ohlcv() -> None:
    exchange = _FakeKraken([], supports_ohlcv=False)
    with pytest.raises(RuntimeError, match="fetchOHLCV"):
        ccxt_ohlcv.fetch_recent_ohlcv("BTC-USD", Timeframe.H4, exchange=exchange)
