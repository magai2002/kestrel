from datetime import UTC, datetime

import pytest

from core.clock import SimClock, WallClock


def test_sim_clock_starts_at_given_time() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    clock = SimClock(start)
    assert clock.now() == start


def test_sim_clock_advances() -> None:
    clock = SimClock(datetime(2024, 1, 1, tzinfo=UTC))
    later = datetime(2024, 1, 2, tzinfo=UTC)
    clock.advance_to(later)
    assert clock.now() == later


def test_sim_clock_rejects_moving_backwards() -> None:
    clock = SimClock(datetime(2024, 1, 2, tzinfo=UTC))
    with pytest.raises(ValueError, match="backwards"):
        clock.advance_to(datetime(2024, 1, 1, tzinfo=UTC))


def test_sim_clock_rejects_naive_start() -> None:
    with pytest.raises(ValueError, match="aware"):
        SimClock(datetime(2024, 1, 1))


def test_sim_clock_rejects_naive_advance() -> None:
    clock = SimClock(datetime(2024, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="aware"):
        clock.advance_to(datetime(2024, 1, 2))


def test_wall_clock_returns_utc_now() -> None:
    clock = WallClock()
    before = datetime.now(UTC)
    result = clock.now()
    after = datetime.now(UTC)
    assert before <= result <= after
    assert result.tzinfo == UTC
