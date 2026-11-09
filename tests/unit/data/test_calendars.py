from datetime import UTC, datetime

import pandas as pd

from data.calendars import crypto_expected_bar_starts, is_nyse_session, nyse_session_dates


def test_nyse_session_dates_excludes_weekends() -> None:
    # 2024-01-06/07 is a Sat/Sun.
    dates = nyse_session_dates(datetime(2024, 1, 5, tzinfo=UTC), datetime(2024, 1, 8, tzinfo=UTC))
    day_strs = {d.strftime("%Y-%m-%d") for d in dates}
    assert "2024-01-05" in day_strs  # Friday
    assert "2024-01-06" not in day_strs
    assert "2024-01-07" not in day_strs
    assert "2024-01-08" in day_strs  # Monday


def test_nyse_session_dates_excludes_known_holiday() -> None:
    # Independence Day (observed).
    dates = nyse_session_dates(datetime(2024, 7, 3, tzinfo=UTC), datetime(2024, 7, 5, tzinfo=UTC))
    day_strs = {d.strftime("%Y-%m-%d") for d in dates}
    assert "2024-07-04" not in day_strs
    assert "2024-07-03" in day_strs
    assert "2024-07-05" in day_strs


def test_is_nyse_session_weekday_vs_weekend() -> None:
    assert is_nyse_session(datetime(2024, 1, 8, tzinfo=UTC)) is True
    assert is_nyse_session(datetime(2024, 1, 6, tzinfo=UTC)) is False


def test_crypto_expected_bar_starts_is_a_continuous_grid() -> None:
    grid = crypto_expected_bar_starts(
        pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2024-01-02", tz="UTC"), "4h"
    )
    assert len(grid) == 6  # 24h / 4h, left-inclusive
    assert grid[0] == pd.Timestamp("2024-01-01", tz="UTC")
