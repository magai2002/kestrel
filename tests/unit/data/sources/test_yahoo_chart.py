"""Parser tests against real, recorded Yahoo chart responses
(tests/fixtures/yahoo/)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pandas as pd
import pytest
import respx

from core.bar import Bar
from core.enums import Timeframe, Venue
from data import BAR_COLUMNS
from data.sources import yahoo_chart

FIXTURES = Path("tests/fixtures/yahoo")
FAR_FUTURE = datetime(2030, 1, 1, tzinfo=UTC)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_parses_spy_into_unified_schema() -> None:
    df = yahoo_chart.parse_chart(_load("SPY-2016-03-dividend.json"), "SPY", "NYSE", FAR_FUTURE)
    assert list(df.columns) == list(BAR_COLUMNS)
    assert len(df) == 8
    assert set(df["venue"]) == {Venue.YAHOO.value}
    # Session open in UTC (09:30 ET = 13:30 UTC in March 2016 after DST).
    assert df["open_time"].iloc[0] == pd.Timestamp("2016-03-15 13:30", tz="UTC")
    # Close from the NYSE calendar, 16:00 ET.
    assert df["close_time"].iloc[0] == pd.Timestamp("2016-03-15 20:00", tz="UTC")


def test_close_is_dividend_adjusted_and_ohl_scaled_by_same_factor() -> None:
    payload = _load("SPY-2016-03-dividend.json")
    result = payload["chart"]["result"][0]
    raw_close = result["indicators"]["quote"][0]["close"][0]
    raw_open = result["indicators"]["quote"][0]["open"][0]
    adj = result["indicators"]["adjclose"][0]["adjclose"][0]
    df = yahoo_chart.parse_chart(payload, "SPY", "NYSE", FAR_FUTURE)
    assert df["close"].iloc[0] == pytest.approx(adj)
    assert df["open"].iloc[0] == pytest.approx(raw_open * adj / raw_close)
    assert adj < raw_close  # a decade of dividends since makes the adjusted price lower


def test_every_row_is_a_valid_bar() -> None:
    df = yahoo_chart.parse_chart(_load("SPY-2016-03-dividend.json"), "SPY", "NYSE", FAR_FUTURE)
    for row in df.itertuples():
        Bar(
            venue=Venue.YAHOO,
            symbol="SPY",
            timeframe=Timeframe.D1,
            open_time=row.open_time,
            close_time=row.close_time,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
        )


def test_drops_the_session_still_trading() -> None:
    """Real response captured mid-session on 2026-11-20: Yahoo returns
    today's bar with partial volume. It must not survive parsing."""
    payload = _load("VUSD.L-2026-11-in-progress.json")
    mid_session = datetime(2026, 11, 20, 11, 0, tzinfo=UTC)
    df = yahoo_chart.parse_chart(payload, "VUSD.L", "LSE", mid_session)
    assert df["open_time"].max() < pd.Timestamp("2026-11-20", tz="UTC")
    assert (df["close_time"] <= pd.Timestamp(mid_session)).all()
    # LSE: 08:00 open, 16:30 close (GMT in November).
    assert df["open_time"].iloc[-1].hour == 8
    assert df["close_time"].iloc[-1] == pd.Timestamp("2026-11-19 16:30", tz="UTC")


def test_error_payload_raises_shape_error() -> None:
    with pytest.raises(yahoo_chart.YahooShapeError, match="error"):
        yahoo_chart.parse_chart(_load("not-found.json"), "NOPE", "NYSE", FAR_FUTURE)


def test_missing_indicators_raises_shape_error() -> None:
    payload = _load("SPY-2016-03-dividend.json")
    del payload["chart"]["result"][0]["indicators"]["adjclose"]
    with pytest.raises(yahoo_chart.YahooShapeError):
        yahoo_chart.parse_chart(payload, "SPY", "NYSE", FAR_FUTURE)


@respx.mock
def test_fetch_404_raises_shape_error() -> None:
    respx.get(url__startswith="https://query1.finance.yahoo.com/v8/finance/chart/NOPE").mock(
        return_value=httpx.Response(404, json=_load("not-found.json"))
    )
    with httpx.Client() as client, pytest.raises(yahoo_chart.YahooShapeError, match="404"):
        yahoo_chart.fetch_daily_bars(
            "NOPE",
            datetime(2024, 1, 1).date(),
            datetime(2024, 2, 1).date(),
            "NYSE",
            client,
            FAR_FUTURE,
        )
