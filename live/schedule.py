"""When does each sleeve's next bar close?

- 24/7 crypto: on the UTC grid of the decision bar (every 4h: 00:00,
  04:00 ... UTC).
- Exchange-traded (the equities sleeve's UCITS twins on the LSE): at
  each session's real close from the exchange calendar, early closes
  included (12:30 London on Christmas Eve).

The runner acts at ``bar_close + bar_close_delay_seconds`` so the venue
has published the closed bar, then *confirms* the bar it expected is
actually there before deciding (``live/sleeve_runner.py``).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from core.enums import Timeframe
from data.calendars import session_schedule


def next_bar_close(after: datetime, timeframe: Timeframe, calendar: str | None) -> datetime:
    """The first bar close strictly after ``after``."""
    if calendar is None:
        step = timeframe.timedelta_seconds
        epoch = int(after.timestamp())
        nxt = (epoch // step + 1) * step
        return datetime.fromtimestamp(nxt, after.tzinfo)
    sched = session_schedule(calendar, after - timedelta(days=1), after + timedelta(days=14))
    closes = pd.DatetimeIndex(sched["market_close"])
    later = closes[closes > pd.Timestamp(after)]
    if len(later) == 0:
        raise RuntimeError(f"no {calendar} session close in the next two weeks after {after}")
    return later[0].to_pydatetime()


def last_bar_close(at_or_before: datetime, timeframe: Timeframe, calendar: str | None) -> datetime:
    """The most recent bar close at or before ``at_or_before``."""
    if calendar is None:
        step = timeframe.timedelta_seconds
        epoch = int(at_or_before.timestamp())
        return datetime.fromtimestamp((epoch // step) * step, at_or_before.tzinfo)
    sched = session_schedule(calendar, at_or_before - timedelta(days=14), at_or_before)
    closes = pd.DatetimeIndex(sched["market_close"])
    earlier = closes[closes <= pd.Timestamp(at_or_before)]
    return earlier[-1].to_pydatetime()


def sessions_between(after: datetime, until: datetime, calendar: str) -> int:
    """Number of session closes in ``(after, until]`` — staleness for a
    daily exchange sleeve is counted in sessions, so a weekend isn't stale."""
    if until <= after:
        return 0
    sched = session_schedule(calendar, after - timedelta(days=1), until)
    closes = pd.DatetimeIndex(sched["market_close"])
    return int(((closes > pd.Timestamp(after)) & (closes <= pd.Timestamp(until))).sum())
