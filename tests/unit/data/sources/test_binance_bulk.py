"""Binance bulk-data parsing and fetching.

The timestamp-unit tests read real downloaded samples committed at
tests/fixtures/binance/ (one 2019 file, known to use milliseconds; one
2025 file, confirmed by direct inspection to use microseconds — see
data/sources/binance_bulk.py's module docstring) rather than synthetic
data, so a regression in the magnitude threshold shows up against
ground truth, not against our own assumptions.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import date
from pathlib import Path

import httpx
import pandas as pd
import pytest
import respx

from core.enums import Timeframe, Venue
from data.sources import binance_bulk

FIXTURES = Path(__file__).parents[3] / "fixtures" / "binance"


def _zip_bytes(csv_content: bytes, inner_name: str = "BTCUSDT-1h-2024-01.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(inner_name, csv_content)
    return buf.getvalue()


def test_detect_timestamp_unit_on_real_2019_sample_is_milliseconds() -> None:
    csv_bytes = (FIXTURES / "BTCUSDT-1h-2019-09-sample.csv").read_bytes()
    first_value = int(csv_bytes.decode().splitlines()[0].split(",")[0])
    assert binance_bulk._detect_timestamp_unit(first_value) == "ms"


def test_detect_timestamp_unit_on_real_2025_sample_is_microseconds() -> None:
    csv_bytes = (FIXTURES / "BTCUSDT-1h-2025-09-sample.csv").read_bytes()
    first_value = int(csv_bytes.decode().splitlines()[0].split(",")[0])
    assert binance_bulk._detect_timestamp_unit(first_value) == "us"


def test_parse_kline_csv_ms_sample_decodes_to_correct_date() -> None:
    csv_bytes = (FIXTURES / "BTCUSDT-1h-2019-09-sample.csv").read_bytes()
    df = binance_bulk.parse_kline_csv(csv_bytes)
    assert df["open_time"].iloc[0] == pd.Timestamp("2019-09-01", tz="UTC")
    assert "close_time" not in df.columns  # dropped; recomputed downstream


def test_parse_kline_csv_us_sample_decodes_to_correct_date() -> None:
    csv_bytes = (FIXTURES / "BTCUSDT-1h-2025-09-sample.csv").read_bytes()
    df = binance_bulk.parse_kline_csv(csv_bytes)
    assert df["open_time"].iloc[0] == pd.Timestamp("2025-09-01", tz="UTC")


def test_verify_checksum_accepts_matching_hash() -> None:
    content = b"hello world"
    digest = hashlib.sha256(content).hexdigest()
    binance_bulk.verify_checksum(content, f"{digest}  somefile.zip", "somefile.zip")  # no raise


def test_verify_checksum_rejects_mismatch() -> None:
    content = b"hello world"
    with pytest.raises(binance_bulk.ChecksumMismatchError):
        binance_bulk.verify_checksum(content, "0" * 64 + "  somefile.zip", "somefile.zip")


@respx.mock
def test_download_monthly_file_returns_none_on_404() -> None:
    spec = binance_bulk.MonthlyFileSpec(symbol="BTCUSDT", interval="1h", year=2099, month=1)
    respx.get(spec.url).mock(return_value=httpx.Response(404))
    with httpx.Client() as client:
        assert binance_bulk.download_monthly_file(spec, client) is None


@respx.mock
def test_download_monthly_file_verifies_and_returns_bytes() -> None:
    csv_bytes = (FIXTURES / "BTCUSDT-1h-2019-09-sample.csv").read_bytes()
    zip_bytes = _zip_bytes(csv_bytes)
    digest = hashlib.sha256(zip_bytes).hexdigest()

    spec = binance_bulk.MonthlyFileSpec(symbol="BTCUSDT", interval="1h", year=2019, month=9)
    respx.get(spec.url).mock(return_value=httpx.Response(200, content=zip_bytes))
    respx.get(spec.checksum_url).mock(
        return_value=httpx.Response(200, text=f"{digest}  {spec.filename}")
    )
    with httpx.Client() as client:
        result = binance_bulk.download_monthly_file(spec, client)
    assert result == zip_bytes


@respx.mock
def test_download_monthly_file_raises_on_checksum_mismatch() -> None:
    csv_bytes = (FIXTURES / "BTCUSDT-1h-2019-09-sample.csv").read_bytes()
    zip_bytes = _zip_bytes(csv_bytes)

    spec = binance_bulk.MonthlyFileSpec(symbol="BTCUSDT", interval="1h", year=2019, month=9)
    respx.get(spec.url).mock(return_value=httpx.Response(200, content=zip_bytes))
    respx.get(spec.checksum_url).mock(
        return_value=httpx.Response(200, text=f"{'0' * 64}  {spec.filename}")
    )
    with httpx.Client() as client, pytest.raises(binance_bulk.ChecksumMismatchError):
        binance_bulk.download_monthly_file(spec, client)


@respx.mock
def test_backfill_klines_skips_404_months_and_concatenates_the_rest() -> None:
    csv_bytes = (FIXTURES / "BTCUSDT-1h-2019-09-sample.csv").read_bytes()
    zip_bytes = _zip_bytes(csv_bytes)
    digest = hashlib.sha256(zip_bytes).hexdigest()

    sept = binance_bulk.MonthlyFileSpec(symbol="BTCUSDT", interval="1h", year=2019, month=9)
    respx.get(sept.url).mock(return_value=httpx.Response(200, content=zip_bytes))
    respx.get(sept.checksum_url).mock(
        return_value=httpx.Response(200, text=f"{digest}  {sept.filename}")
    )

    october = binance_bulk.MonthlyFileSpec(symbol="BTCUSDT", interval="1h", year=2019, month=10)
    respx.get(october.url).mock(return_value=httpx.Response(404))

    df = binance_bulk.backfill_klines("BTCUSDT", "1h", date(2019, 9, 1), date(2019, 10, 31))

    assert not df.empty
    assert set(df["venue"]) == {Venue.BINANCE.value}
    assert set(df["symbol"]) == {"BTCUSDT"}
    assert set(df["timeframe"]) == {"1h"}
    assert df["open_time"].is_monotonic_increasing
    assert df["open_time"].duplicated().sum() == 0
    # close_time must be recomputed (open_time + 1h), not Binance's raw inclusive endpoint.
    assert (df["close_time"] - df["open_time"] == pd.Timedelta(hours=1)).all()


def _unified_hourly(n: int, start: str) -> pd.DataFrame:
    open_time = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "venue": "binance",
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "open_time": open_time,
            "close_time": open_time + pd.Timedelta(hours=1),
            "open": [100.0 + i for i in range(n)],
            "high": [100.0 + i + 1 for i in range(n)],
            "low": [100.0 + i - 1 for i in range(n)],
            "close": [100.0 + i + 0.5 for i in range(n)],
            "volume": [1.0 for _ in range(n)],
        }
    )


def test_resample_aggregates_ohlcv_correctly() -> None:
    df = _unified_hourly(4, "2024-01-01 00:00")
    result = binance_bulk.resample(df, Timeframe.H4, Timeframe.H1.timedelta_seconds)
    assert len(result) == 1
    row = result.iloc[0]
    assert row["open"] == df["open"].iloc[0]
    assert row["close"] == df["close"].iloc[-1]
    assert row["high"] == df["high"].max()
    assert row["low"] == df["low"].min()
    assert row["volume"] == df["volume"].sum()
    assert row["close_time"] - row["open_time"] == pd.Timedelta(hours=4)


def test_resample_drops_incomplete_trailing_bucket() -> None:
    df = _unified_hourly(5, "2024-01-01 00:00")  # 1 full 4h bucket + 1 leftover hour
    result = binance_bulk.resample(df, Timeframe.H4, Timeframe.H1.timedelta_seconds)
    assert len(result) == 1
