"""Daily ETF bars from Yahoo Finance's public chart endpoint — the
free, no-account half of the "hybrid" equities data decision (see
``docs/adr/0004-ibkr-ucits-hybrid-data.md``): the 2016+ research
backfill for the US ETFs, plus the warm-up history for the UCITS twins
the bot actually trades through IBKR.

**Unofficial endpoint, so treated as hostile** (spec Section 2: "free
third-party APIs change or disappear"): every field this module reads
is checked and a missing one raises ``YahooShapeError`` rather than
silently producing NaNs. Real responses are committed under
``tests/fixtures/yahoo/`` and the parser is tested against them.

Verified against real responses, not assumed (``docs/api-notes.md``):

- ``GET https://query1.finance.yahoo.com/v8/finance/chart/{symbol}``
  with ``period1``/``period2`` (epoch seconds), ``interval=1d``,
  ``events=div,split``. A plain browser User-Agent is enough; Stooq,
  the other obvious free source, now sits behind a JavaScript
  proof-of-work wall and is unusable from a server.
- **Daily timestamps are the session *open*** in UTC (13:30/14:30 for
  NYSE depending on DST, 08:00 for LSE) — not midnight, not the close.
  We keep it as ``open_time`` and take ``close_time`` from the real
  exchange calendar (``data/calendars.py``), so early-close days
  (e.g. 13:00 ET the day after Thanksgiving) get their true close.
- **The current session is returned while it is still trading**
  (observed: a VUSD.L bar stamped today with a fraction of normal
  volume, mid-session). Design rule 3 forbids acting on it, so any bar
  whose session close is after ``now`` is dropped.
- ``indicators.adjclose`` is the split- *and* dividend-adjusted close.
  We scale open/high/low by the same per-bar factor
  (``adjclose / close``), which is the standard way to get a full
  adjusted OHLC series out of a close-only adjustment.
- An unknown symbol returns HTTP 404 with
  ``{"chart": {"result": null, "error": {"code": "Not Found", ...}}}``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any

import httpx
import pandas as pd

from core.enums import Venue
from data import BAR_COLUMNS
from data.calendars import session_schedule

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
# A generic browser UA: the endpoint rejects some default library UAs.
# Deliberately carries no project/user identity.
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64)"


class YahooShapeError(RuntimeError):
    """The response no longer looks like the shape this module was written against."""


def fetch_chart_json(symbol: str, start: date, end: date, client: httpx.Client) -> dict[str, Any]:
    params: dict[str, str | int] = {
        "period1": int(datetime.combine(start, time.min, tzinfo=UTC).timestamp()),
        "period2": int(datetime.combine(end, time.max, tzinfo=UTC).timestamp()),
        "interval": "1d",
        "events": "div,split",
    }
    response = client.get(
        CHART_URL.format(symbol=symbol), params=params, headers={"User-Agent": USER_AGENT}
    )
    if response.status_code == 404:
        raise YahooShapeError(f"Yahoo has no data for {symbol!r} (HTTP 404)")
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    return payload


def parse_chart(
    payload: dict[str, Any],
    store_symbol: str,
    calendar: str,
    now: datetime,
) -> pd.DataFrame:
    """Turn one chart response into the unified bar schema.

    ``store_symbol`` is what the bars are stored under (``SPY``,
    ``VUSD.L``); ``calendar`` is a ``pandas_market_calendars`` name
    (``NYSE``/``LSE``) used for real session close times and to drop
    the still-trading session.
    """
    chart = payload.get("chart")
    if not isinstance(chart, dict):
        raise YahooShapeError("missing top-level 'chart' object")
    if chart.get("error"):
        raise YahooShapeError(f"Yahoo returned an error: {chart['error']}")
    results = chart.get("result")
    if not results:
        raise YahooShapeError("'chart.result' is empty")
    result = results[0]

    timestamps = result.get("timestamp")
    if not timestamps:
        return pd.DataFrame(columns=list(BAR_COLUMNS))
    try:
        quote = result["indicators"]["quote"][0]
        adjclose = result["indicators"]["adjclose"][0]["adjclose"]
        raw = pd.DataFrame(
            {
                "ts": timestamps,
                "open": quote["open"],
                "high": quote["high"],
                "low": quote["low"],
                "close": quote["close"],
                "volume": quote["volume"],
                "adjclose": adjclose,
            }
        )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise YahooShapeError(f"unexpected indicators shape: {exc!r}") from exc

    # Yahoo emits nulls for sessions with no trade print; nothing to keep.
    raw = raw.dropna(subset=["open", "high", "low", "close", "adjclose"])
    if raw.empty:
        return pd.DataFrame(columns=list(BAR_COLUMNS))

    open_time = pd.to_datetime(raw["ts"], unit="s", utc=True)
    session_dates = open_time.dt.tz_convert(None).dt.normalize()
    schedule = session_schedule(calendar, session_dates.min(), session_dates.max())
    close_by_date = pd.Series(
        schedule["market_close"].to_numpy(), index=pd.DatetimeIndex(schedule.index).normalize()
    )
    close_time = session_dates.map(close_by_date)
    if close_time.isna().any():
        bad = list(open_time[close_time.isna()].dt.date.astype(str))[:5]
        raise YahooShapeError(f"bars on non-{calendar}-session dates: {bad}")

    factor = raw["adjclose"] / raw["close"]
    frame = pd.DataFrame(
        {
            "venue": Venue.YAHOO.value,
            "symbol": store_symbol,
            "timeframe": "1d",
            "open_time": open_time.to_numpy(),
            "close_time": pd.to_datetime(close_time.to_numpy(), utc=True),
            "open": (raw["open"] * factor).astype(float).to_numpy(),
            "high": (raw["high"] * factor).astype(float).to_numpy(),
            "low": (raw["low"] * factor).astype(float).to_numpy(),
            "close": raw["adjclose"].astype(float).to_numpy(),
            "volume": raw["volume"].fillna(0).astype(float).to_numpy(),
        }
    )
    frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True)
    # Never keep a session that hasn't closed yet (design rule 3).
    frame = frame[frame["close_time"] <= pd.Timestamp(now)]
    # Rounding in Yahoo's float32-ish fields can put a scaled high a hair
    # under the close (or low a hair over); clamp so core.bar.Bar's OHLC
    # consistency check doesn't reject an otherwise-fine bar.
    frame["high"] = frame[["open", "high", "low", "close"]].max(axis=1)
    frame["low"] = frame[["open", "high", "low", "close"]].min(axis=1)
    return frame.drop_duplicates(subset=["open_time"]).reset_index(drop=True)[list(BAR_COLUMNS)]


def fetch_daily_bars(
    symbol: str,
    start: date,
    end: date,
    calendar: str,
    client: httpx.Client,
    now: datetime,
    store_symbol: str | None = None,
) -> pd.DataFrame:
    payload = fetch_chart_json(symbol, start, end, client)
    return parse_chart(payload, store_symbol or symbol, calendar, now)
