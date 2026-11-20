"""Session calendars: NYSE for equities, 24/7 for crypto.

Uses ``pandas_market_calendars`` rather than ``exchange_calendars``
(the spec names either as acceptable) — it's the lighter dependency and
its ``schedule()`` output converts straight to the UTC close timestamps
we need to validate equity bar alignment against.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pandas_market_calendars as mcal

_NYSE = mcal.get_calendar("NYSE")


def nyse_session_closes(start: datetime, end: datetime) -> pd.DatetimeIndex:
    """UTC close timestamps for every NYSE session in ``[start, end]``.

    Used by ``data.validate`` to check that a daily equity bar exists
    for every session and that no bar exists for a non-session day.
    """
    schedule = _NYSE.schedule(start_date=start, end_date=end)
    closes = pd.DatetimeIndex(schedule["market_close"])
    return closes.tz_convert("UTC")


def is_nyse_session(day: datetime) -> bool:
    valid_days = _NYSE.valid_days(start_date=day, end_date=day)
    return len(valid_days) > 0


def nyse_session_dates(start: datetime, end: datetime) -> pd.DatetimeIndex:
    """Calendar dates (midnight UTC, no time-of-day) of every NYSE
    session in ``[start, end]``.

    Deliberately date-only, not a bar timestamp grid: we don't yet know
    (no real Alpaca data fetched) whether its daily-bar timestamps are
    stamped at session open, session close or UTC midnight, so
    ``data.validate`` compares by calendar date rather than assuming a
    specific time-of-day convention. See ``docs/api-notes.md``.
    """
    valid_days = _NYSE.valid_days(start_date=start, end_date=end)
    return pd.DatetimeIndex(valid_days).tz_convert("UTC").normalize()


def session_schedule(calendar: str, start: datetime, end: datetime) -> pd.DataFrame:
    """``market_open``/``market_close`` (UTC) per session for any
    ``pandas_market_calendars`` calendar (``NYSE``, ``LSE``, ...).

    Index is the session date (tz-naive midnight). Includes early
    closes (e.g. NYSE 18:00 UTC the day after Thanksgiving, LSE 12:30 on
    Christmas Eve), which is why equity bar close times come from here
    and are never computed as ``open_time + 1 day``.
    """
    schedule: pd.DataFrame = mcal.get_calendar(calendar).schedule(start_date=start, end_date=end)
    return pd.DataFrame(
        {
            "market_open": pd.DatetimeIndex(schedule["market_open"]).tz_convert("UTC"),
            "market_close": pd.DatetimeIndex(schedule["market_close"]).tz_convert("UTC"),
        },
        index=schedule.index,
    )


def crypto_expected_bar_starts(start: datetime, end: datetime, freq: str) -> pd.DatetimeIndex:
    """Expected bar-open timestamps for a 24/7 crypto market.

    Crypto never closes, so this is just a regular UTC grid at ``freq``
    (e.g. ``"1h"``, ``"4h"``) — no calendar needed.
    """
    return pd.date_range(start=start, end=end, freq=freq, tz="UTC", inclusive="left")
