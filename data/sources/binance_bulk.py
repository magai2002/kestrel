"""Binance public bulk kline data (``data.binance.vision``).

See ``docs/api-notes.md`` for what we verified by downloading real
files rather than trusting the README:

- Monthly ZIPs live at
  ``{BASE_URL}/data/spot/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{YYYY}-{MM}.zip``,
  each with a sibling ``.CHECKSUM`` file in ``sha256sum -c`` format
  (``"<hex>  <filename>"``).
- The CSV inside has **no header row** and exactly the 12 documented
  kline columns.
- **Timestamp unit is not fixed.** Files from 2019 use millisecond
  epoch timestamps (13 digits); files from 2025 use *microseconds*
  (16 digits) with no flag anywhere in the file or its name saying so.
  We detect it by magnitude: anything at or above ``1e14`` is
  microseconds (a millisecond timestamp doesn't reach ``1e14`` until
  the year 5138; a microsecond timestamp passes it for any date after
  1973), confirmed against real downloaded samples in
  ``tests/fixtures/binance/``.
"""

from __future__ import annotations

import hashlib
import io
import logging
import zipfile
from dataclasses import dataclass
from datetime import date
from typing import Literal

import httpx
import pandas as pd

from core.enums import Timeframe, Venue

logger = logging.getLogger(__name__)

BASE_URL = "https://data.binance.vision"

# Documented order for a spot kline row (Binance REST + bulk-data CSV agree on this).
_COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_asset_volume",
    "num_trades",
    "taker_buy_base",
    "taker_buy_quote",
    "ignore",
)

# A millisecond epoch timestamp passes this threshold only in the year
# 5138; a microsecond one passes it for any date after 1973. Comfortable
# margin either way — see module docstring.
_MICROSECOND_THRESHOLD = 1e14


class ChecksumMismatchError(RuntimeError):
    """The downloaded ZIP's SHA-256 didn't match its .CHECKSUM file."""


@dataclass(frozen=True)
class MonthlyFileSpec:
    symbol: str
    interval: str
    year: int
    month: int

    @property
    def filename(self) -> str:
        return f"{self.symbol}-{self.interval}-{self.year:04d}-{self.month:02d}.zip"

    @property
    def url(self) -> str:
        return f"{BASE_URL}/data/spot/monthly/klines/{self.symbol}/{self.interval}/{self.filename}"

    @property
    def checksum_url(self) -> str:
        return f"{self.url}.CHECKSUM"


def _detect_timestamp_unit(sample_value: int) -> Literal["us", "ms"]:
    """Return ``"us"`` or ``"ms"`` by magnitude. See module docstring."""
    return "us" if sample_value >= _MICROSECOND_THRESHOLD else "ms"


def verify_checksum(content: bytes, checksum_text: str, filename: str) -> None:
    """Raise :class:`ChecksumMismatchError` unless ``content`` matches the
    ``.CHECKSUM`` file's ``sha256sum -c``-style line."""
    expected = checksum_text.strip().split()[0]
    actual = hashlib.sha256(content).hexdigest()
    if expected.lower() != actual.lower():
        raise ChecksumMismatchError(f"{filename}: expected sha256 {expected}, got {actual}")


def parse_kline_csv(csv_bytes: bytes) -> pd.DataFrame:
    """Parse one kline CSV (no header) into a DataFrame with UTC
    timestamps, auto-detecting the millisecond/microsecond unit."""
    df = pd.read_csv(io.BytesIO(csv_bytes), header=None, names=_COLUMNS)
    if df.empty:
        return df
    unit = _detect_timestamp_unit(int(df["open_time"].iloc[0]))
    df["open_time"] = pd.to_datetime(df["open_time"], unit=unit, utc=True)
    # Binance's own close_time is one tick before the next open (e.g.
    # ...:59.999999), an inclusive endpoint. We don't use it: callers
    # recompute close_time as open_time + interval instead, so it means
    # the same thing as core.bar.Bar.close_time ("when the bar finished
    # forming") everywhere in this codebase, not just here.
    df = df.drop(columns=["close_time"])
    return df


def download_monthly_file(spec: MonthlyFileSpec, client: httpx.Client) -> bytes | None:
    """Download and checksum-verify one monthly ZIP.

    Returns ``None`` (after logging) if the file 404s — that's the
    expected shape for a month that hasn't been published yet, not an
    error worth crashing the whole backfill over.
    """
    response = client.get(spec.url)
    if response.status_code == 404:
        logger.info(
            "no monthly file for %s %s-%02d (not yet published?)",
            spec.symbol,
            spec.year,
            spec.month,
        )
        return None
    response.raise_for_status()

    checksum_response = client.get(spec.checksum_url)
    checksum_response.raise_for_status()
    verify_checksum(response.content, checksum_response.text, spec.filename)
    return response.content


def extract_csv_from_zip(zip_bytes: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        if len(names) != 1:
            raise ValueError(f"expected exactly one file in zip, got {names}")
        return zf.read(names[0])


def fetch_month(
    symbol: str, interval: str, year: int, month: int, client: httpx.Client
) -> pd.DataFrame | None:
    """Fetch, verify and parse one month of klines. ``None`` if unpublished."""
    spec = MonthlyFileSpec(symbol=symbol, interval=interval, year=year, month=month)
    zip_bytes = download_monthly_file(spec, client)
    if zip_bytes is None:
        return None
    return parse_kline_csv(extract_csv_from_zip(zip_bytes))


def _month_range(start: date, end: date) -> list[tuple[int, int]]:
    months = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append((year, month))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return months


def backfill_klines(
    symbol: str,
    interval: str,
    start: date,
    end: date,
    client: httpx.Client | None = None,
) -> pd.DataFrame:
    """Fetch every available monthly file for ``symbol``/``interval``
    between ``start`` and ``end`` (inclusive months), concatenated and
    tagged with the unified bar schema (venue, symbol, timeframe
    columns; raw kline-specific columns dropped).

    Months that 404 (not yet published — typically the most recent one
    or two) are skipped with a log line, not an exception: the caller's
    validation report is the place a human finds out about the
    resulting gap.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    frames: list[pd.DataFrame] = []
    try:
        for year, month in _month_range(start, end):
            month_df = fetch_month(symbol, interval, year, month, client)
            if month_df is not None and not month_df.empty:
                frames.append(month_df)
    finally:
        if owns_client:
            client.close()

    if not frames:
        return _empty_bar_frame(symbol, interval)

    raw = pd.concat(frames, ignore_index=True)
    raw = raw.drop_duplicates(subset="open_time").sort_values("open_time")
    return _to_unified_schema(raw, symbol, interval)


def _empty_bar_frame(symbol: str, interval: str) -> pd.DataFrame:
    from data import BAR_COLUMNS

    return pd.DataFrame(columns=list(BAR_COLUMNS))


def _to_unified_schema(raw: pd.DataFrame, symbol: str, interval: str) -> pd.DataFrame:
    duration = pd.Timedelta(seconds=Timeframe(interval).timedelta_seconds)
    return pd.DataFrame(
        {
            "venue": Venue.BINANCE.value,
            "symbol": symbol,
            "timeframe": interval,
            "open_time": raw["open_time"],
            "close_time": raw["open_time"] + duration,
            "open": raw["open"].astype(float),
            "high": raw["high"].astype(float),
            "low": raw["low"].astype(float),
            "close": raw["close"].astype(float),
            "volume": raw["volume"].astype(float),
        }
    )


def resample(df: pd.DataFrame, target: Timeframe, source_timeframe_seconds: int) -> pd.DataFrame:
    """Resample 1h bars up to 4h or 1d by proper OHLCV aggregation.

    Drops any trailing partial bucket (fewer constituent bars than a
    full period requires) rather than emit a bar built from incomplete
    data — design rule 3's no-look-ahead spirit applies to storage too.
    """
    if df.empty:
        return df
    expected_count = target.timedelta_seconds // source_timeframe_seconds

    indexed = df.set_index("open_time").sort_index()
    agg = indexed.resample(target.pandas_freq, label="left", closed="left").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    counts = indexed["open"].resample(target.pandas_freq, label="left", closed="left").count()
    agg = agg.loc[counts == expected_count].dropna()

    result = agg.reset_index()
    result["close_time"] = result["open_time"] + pd.Timedelta(seconds=target.timedelta_seconds)
    result["venue"] = df["venue"].iloc[0]
    result["symbol"] = df["symbol"].iloc[0]
    result["timeframe"] = target.value

    from data import BAR_COLUMNS

    return result[list(BAR_COLUMNS)]
