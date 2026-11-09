from datetime import UTC, datetime

import numpy as np
import pandas as pd

from core.enums import Timeframe
from data import validate
from data.calendars import crypto_expected_bar_starts


def _clean_hourly(n: int = 48, start: str = "2024-01-01") -> pd.DataFrame:
    open_time = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    close = 100 + np.cumsum(np.random.default_rng(0).normal(0, 0.1, n))
    return pd.DataFrame(
        {
            "venue": "binance",
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "open_time": open_time,
            "close_time": open_time + pd.Timedelta(hours=1),
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 10.0,
        }
    )


def test_clean_series_has_no_issues() -> None:
    df = _clean_hourly()
    expected = crypto_expected_bar_starts(
        df["open_time"].min(), df["open_time"].max() + pd.Timedelta(hours=1), "1h"
    )
    report = validate.validate_series(
        df, "binance", "BTCUSDT", Timeframe.H1, expected, now=datetime(2030, 1, 1, tzinfo=UTC)
    )
    assert report.is_clean, report.format()


def test_find_duplicates() -> None:
    df = _clean_hourly()
    dup_row = df.iloc[[5]]
    df_with_dupe = pd.concat([df, dup_row], ignore_index=True)
    dupes = validate.find_duplicates(df_with_dupe)
    assert df.loc[5, "open_time"] in dupes


def test_find_zero_volume() -> None:
    df = _clean_hourly()
    df.loc[3, "volume"] = 0.0
    zeros = validate.find_zero_volume(df)
    assert df.loc[3, "open_time"] in zeros


def test_find_misaligned_bars() -> None:
    df = _clean_hourly()
    df.loc[2, "open_time"] = df.loc[2, "open_time"] + pd.Timedelta(minutes=17)
    misaligned = validate.find_misaligned_bars(df, Timeframe.H1)
    assert len(misaligned) == 1


def test_find_gaps_detects_missing_run() -> None:
    df = _clean_hourly(n=48)
    # drop a contiguous 3-hour run
    df = df.drop(df.index[10:13]).reset_index(drop=True)
    expected = crypto_expected_bar_starts(
        pd.Timestamp("2024-01-01", tz="UTC"),
        pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(hours=48),
        "1h",
    )
    gaps = validate.find_gaps(df, expected)
    assert len(gaps) == 1
    start, end = gaps[0]
    assert end - start == pd.Timedelta(hours=2)  # 3 missing bars spanning 2h


def test_find_gaps_detects_two_separate_runs() -> None:
    df = _clean_hourly(n=48)
    df = df.drop(df.index[list(range(10, 12)) + list(range(30, 33))]).reset_index(drop=True)
    expected = crypto_expected_bar_starts(
        pd.Timestamp("2024-01-01", tz="UTC"),
        pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(hours=48),
        "1h",
    )
    gaps = validate.find_gaps(df, expected)
    assert len(gaps) == 2


def test_find_outlier_returns_too_few_bars_returns_empty() -> None:
    df = _clean_hourly(n=2)
    assert validate.find_outlier_returns(df, Timeframe.H1) == []


def test_find_outlier_returns_constant_prices_returns_empty() -> None:
    df = _clean_hourly(n=10)
    df["close"] = 100.0  # zero MAD -- no outliers possible, not a divide-by-zero crash
    assert validate.find_outlier_returns(df, Timeframe.H1) == []


def test_detect_incomplete_last_bar_empty_df_is_false() -> None:
    assert validate.detect_incomplete_last_bar(pd.DataFrame(columns=["close_time"])) is False


def test_validate_equity_series_reports_missing_session_as_gap() -> None:
    df = pd.DataFrame(
        {
            "open_time": [pd.Timestamp("2024-01-02", tz="UTC")],
            "close_time": [pd.Timestamp("2024-01-03", tz="UTC")],
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.5],
            "volume": [1000.0],
        }
    )
    sessions = pd.DatetimeIndex(
        [pd.Timestamp("2024-01-02", tz="UTC"), pd.Timestamp("2024-01-03", tz="UTC")]
    )
    report = validate.validate_equity_series(
        df, "alpaca", "SPY", Timeframe.D1, sessions, now=datetime(2030, 1, 1, tzinfo=UTC)
    )
    assert not report.is_clean
    assert len(report.gaps) == 1
    assert report.misaligned_bars == []


def test_report_format_renders_every_issue_kind() -> None:
    df = _clean_hourly(n=10)
    dup_row = df.iloc[[2]]
    df = pd.concat([df, dup_row], ignore_index=True)
    df.loc[3, "volume"] = 0.0
    df.loc[1, "open_time"] = df.loc[1, "open_time"] + pd.Timedelta(minutes=5)
    expected = crypto_expected_bar_starts(
        pd.Timestamp("2024-01-01", tz="UTC"),
        pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(hours=20),
        "1h",
    )
    report = validate.validate_series(
        df, "binance", "BTCUSDT", Timeframe.H1, expected, now=datetime(2000, 1, 1, tzinfo=UTC)
    )
    text = report.format()
    assert "duplicates: 1" in text
    assert "gaps:" in text
    assert "zero-volume bars: 1" in text
    assert "misaligned bars: 1" in text
    assert "incomplete" in text


def test_find_outlier_returns_flags_spike() -> None:
    df = _clean_hourly(n=48)
    df.loc[20, "close"] = df.loc[20, "close"] * 5  # a 5x spike
    outliers = validate.find_outlier_returns(df, Timeframe.H1)
    assert len(outliers) >= 1


def test_find_outlier_returns_is_local_not_global() -> None:
    """A regime-shift series where ordinary moves in a newly-volatile
    era shouldn't all trip a check calibrated on an earlier calm era.
    Direct regression for the false-positive flood seen running Phase
    0's real BTC/ETH backfill (see find_outlier_returns's docstring).

    Right at the transition, a rolling window is still partly filled
    with calm-era history, so some elevated flagging there is expected
    and correct (a sudden vol regime change is itself notable). What
    must *not* happen is the volatile regime staying flagged once
    enough of its own history has rolled into the window -- that would
    mean the window never recalibrates, i.e. it's effectively global.
    """
    window_days = 5
    window_bars = window_days * 24
    n_calm, n_volatile = window_bars * 4, window_bars * 4
    open_time = pd.date_range("2019-01-01", periods=n_calm + n_volatile, freq="1h", tz="UTC")
    calm_closes = 100 + np.cumsum(np.random.default_rng(1).normal(0, 0.05, n_calm))
    volatile_closes = calm_closes[-1] + np.cumsum(
        np.random.default_rng(2).normal(0, 2.0, n_volatile)  # ~40x more volatile
    )
    close = np.concatenate([calm_closes, volatile_closes])
    df = pd.DataFrame(
        {
            "venue": "binance",
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "open_time": open_time,
            "close_time": open_time + pd.Timedelta(hours=1),
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 10.0,
        }
    )

    outliers = validate.find_outlier_returns(df, Timeframe.H1, window_days=window_days)
    # Once a point is more than half a window past the transition, its
    # centered window is entirely volatile-era data -- the "deep" part
    # of the new regime, where recalibration should be complete.
    deep_volatile_start = df["open_time"].iloc[n_calm + window_bars]
    deep_volatile_end = df["open_time"].iloc[n_calm + n_volatile - window_bars]
    deep_outliers = [ts for ts, _ in outliers if deep_volatile_start <= ts <= deep_volatile_end]
    n_deep_bars = n_volatile - 2 * window_bars
    assert len(deep_outliers) < n_deep_bars * 0.1


def test_detect_incomplete_last_bar_true_when_close_in_future() -> None:
    df = _clean_hourly(n=5)
    now = df["close_time"].iloc[2]  # earlier than the last bar's close_time
    assert validate.detect_incomplete_last_bar(df, now=now) is True


def test_detect_incomplete_last_bar_false_when_closed() -> None:
    df = _clean_hourly(n=5)
    now = df["close_time"].max() + pd.Timedelta(hours=1)
    assert validate.detect_incomplete_last_bar(df, now=now) is False


def test_report_format_mentions_issue_counts() -> None:
    df = _clean_hourly()
    df.loc[0, "volume"] = 0.0
    report = validate.validate_series(
        df, "binance", "BTCUSDT", Timeframe.H1, now=datetime(2030, 1, 1, tzinfo=UTC)
    )
    text = report.format()
    assert "zero-volume bars: 1" in text


def test_find_missing_sessions_compares_by_date_not_exact_timestamp() -> None:
    # Bars stamped at market close (20:00 UTC), not midnight -- the
    # exact convention Alpaca actually uses isn't verified yet (see
    # data/calendars.py docstring), so this must still match by date.
    session_dates = pd.DatetimeIndex(
        [pd.Timestamp("2024-01-02", tz="UTC"), pd.Timestamp("2024-01-03", tz="UTC")]
    )
    df = pd.DataFrame({"open_time": [pd.Timestamp("2024-01-02 20:00", tz="UTC")]})
    missing = validate.find_missing_sessions(df, session_dates)
    assert missing == [(pd.Timestamp("2024-01-03", tz="UTC"), pd.Timestamp("2024-01-03", tz="UTC"))]
