from pathlib import Path

import pandas as pd
import pytest

from data import BAR_COLUMNS, store


def _make_bars(venue: str, symbol: str, timeframe: str, n: int, start: str) -> pd.DataFrame:
    open_time = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "venue": venue,
            "symbol": symbol,
            "timeframe": timeframe,
            "open_time": open_time,
            "close_time": open_time + pd.Timedelta(hours=1),
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.5 + i for i in range(n)],
            "volume": [10.0 for _ in range(n)],
        }
    )


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    df = _make_bars("binance", "BTCUSDT", "1h", 5, "2024-01-01")
    store.write_bars(df, tmp_path)
    result = store.read_bars(tmp_path, venue="binance", symbol="BTCUSDT", timeframe="1h")

    assert list(result.columns) == list(BAR_COLUMNS)
    assert len(result) == 5
    pd.testing.assert_series_equal(
        result["close"].reset_index(drop=True), df["close"].reset_index(drop=True)
    )


def test_read_empty_store_returns_empty_frame_with_correct_columns(tmp_path: Path) -> None:
    result = store.read_bars(tmp_path)
    assert result.empty
    assert list(result.columns) == list(BAR_COLUMNS)


def test_read_filters_by_venue_symbol_timeframe(tmp_path: Path) -> None:
    store.write_bars(_make_bars("binance", "BTCUSDT", "1h", 3, "2024-01-01"), tmp_path)
    store.write_bars(_make_bars("binance", "ETHUSDT", "1h", 3, "2024-01-01"), tmp_path)

    btc_only = store.read_bars(tmp_path, symbol="BTCUSDT")
    assert set(btc_only["symbol"]) == {"BTCUSDT"}
    assert len(btc_only) == 3


def test_write_splits_across_year_partitions(tmp_path: Path) -> None:
    # 5 bars/hour starting a few hours before year-end crosses into the next year.
    df = _make_bars("binance", "BTCUSDT", "1h", 5, "2023-12-31 22:00")
    written = store.write_bars(df, tmp_path)
    years = {p.parent.name for p in written}
    assert years == {"year=2023", "year=2024"}

    result = store.read_bars(tmp_path)
    assert len(result) == 5


def test_rewriting_a_partition_overwrites_not_appends(tmp_path: Path) -> None:
    df_v1 = _make_bars("binance", "BTCUSDT", "1h", 3, "2024-01-01")
    store.write_bars(df_v1, tmp_path)
    df_v2 = _make_bars("binance", "BTCUSDT", "1h", 3, "2024-01-01")
    df_v2["close"] = 999.0
    store.write_bars(df_v2, tmp_path)

    result = store.read_bars(tmp_path)
    assert len(result) == 3  # not 6
    assert (result["close"] == 999.0).all()


def test_rejects_missing_columns(tmp_path: Path) -> None:
    df = _make_bars("binance", "BTCUSDT", "1h", 3, "2024-01-01").drop(columns=["volume"])
    with pytest.raises(ValueError, match="missing columns"):
        store.write_bars(df, tmp_path)


def test_rejects_symbol_containing_slash(tmp_path: Path) -> None:
    df = _make_bars("kraken", "BTC/USD", "4h", 3, "2024-01-01")
    with pytest.raises(ValueError, match="/"):
        store.write_bars(df, tmp_path)
