"""The ``Clock`` protocol (design rule 2): the only place outside
``live/runner.py`` that may read the system time.

``SimClock`` only ever advances when told to — the backtest engine
calls ``advance_to`` as each bar closes — so nothing downstream can
observe a timestamp ahead of the data it's actually been given. This
makes the clock itself part of the no-look-ahead guarantee (design rule
3), not just something strategies are trusted to respect.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SimClock:
    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("SimClock start must be timezone-aware")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance_to(self, timestamp: datetime) -> None:
        if timestamp.tzinfo is None:
            raise ValueError("SimClock can only advance to a timezone-aware timestamp")
        if timestamp < self._now:
            raise ValueError(f"clock cannot move backwards: {timestamp} < {self._now}")
        self._now = timestamp


class WallClock:
    """Live/sim/sandbox modes (Phase 4): the real system clock, UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)
