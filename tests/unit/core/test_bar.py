from datetime import UTC, datetime, timezone

import pytest
from pydantic import ValidationError

from core.bar import Bar
from core.enums import Timeframe, Venue


def _bar(**overrides: object) -> Bar:
    defaults = dict(
        venue=Venue.BINANCE,
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        open_time=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        close_time=datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
        open=100.0,
        high=105.0,
        low=99.0,
        close=103.0,
        volume=10.0,
    )
    defaults.update(overrides)
    return Bar(**defaults)


def test_valid_bar_constructs() -> None:
    bar = _bar()
    assert bar.instrument_id == "binance:BTCUSDT"


def test_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        _bar(open_time=datetime(2024, 1, 1, 0, 0))


def test_rejects_non_utc_timezone() -> None:
    from datetime import timedelta

    tz = timezone(timedelta(hours=-5))
    with pytest.raises(ValidationError):
        _bar(open_time=datetime(2024, 1, 1, 0, 0, tzinfo=tz))


def test_rejects_close_before_open() -> None:
    with pytest.raises(ValidationError):
        _bar(
            open_time=datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
            close_time=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        )


def test_rejects_high_below_body() -> None:
    with pytest.raises(ValidationError):
        _bar(high=50.0)


def test_rejects_low_above_body() -> None:
    # low=104 alone (open=100, close=103, high=105) satisfies the high
    # check (105 >= max(100, 103, 104)) so this exercises the low
    # check specifically, not the high check tripping first.
    with pytest.raises(ValidationError):
        _bar(low=104.0)


def test_rejects_negative_volume() -> None:
    with pytest.raises(ValidationError):
        _bar(volume=-1.0)


def test_bar_is_immutable() -> None:
    bar = _bar()
    with pytest.raises(ValidationError):
        bar.close = 999.0  # type: ignore[misc]
