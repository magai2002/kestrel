"""Per-symbol data-quality checks (spec Section Phase 0: "gaps,
duplicates, outliers, timezone and bar-alignment checks, incomplete-last-bar
detection").

Each check is a small, independently-testable function; ``validate_series``
composes them into one :class:`ValidationReport`. Nothing here decides
whether a gap is *acceptable* (some are — see ``docs/progress.md`` for
documented known gaps) — that judgment call belongs to whoever reads the
report, human or CLI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from core.enums import Timeframe


@dataclass(frozen=True)
class ValidationReport:
    venue: str
    symbol: str
    timeframe: str
    n_bars: int
    duplicate_open_times: list[pd.Timestamp] = field(default_factory=list)
    gaps: list[tuple[pd.Timestamp, pd.Timestamp]] = field(default_factory=list)
    zero_volume_bars: list[pd.Timestamp] = field(default_factory=list)
    outlier_returns: list[tuple[pd.Timestamp, float]] = field(default_factory=list)
    misaligned_bars: list[pd.Timestamp] = field(default_factory=list)
    incomplete_last_bar: bool = False

    @property
    def is_clean(self) -> bool:
        return not (
            self.duplicate_open_times
            or self.gaps
            or self.zero_volume_bars
            or self.outlier_returns
            or self.misaligned_bars
            or self.incomplete_last_bar
        )

    def format(self) -> str:
        header = f"{self.venue}:{self.symbol} ({self.timeframe}), {self.n_bars} bars"
        if self.is_clean:
            return f"[clean] {header}"
        lines = [f"[ISSUES] {header}"]
        if self.duplicate_open_times:
            lines.append(f"  duplicates: {len(self.duplicate_open_times)}")
        if self.gaps:
            lines.append(f"  gaps: {len(self.gaps)}")
            for gap_start, gap_end in self.gaps[:5]:
                lines.append(f"    missing {gap_start} .. {gap_end}")
            if len(self.gaps) > 5:
                lines.append(f"    ... and {len(self.gaps) - 5} more")
        if self.zero_volume_bars:
            lines.append(f"  zero-volume bars: {len(self.zero_volume_bars)}")
        if self.outlier_returns:
            lines.append(f"  outlier returns: {len(self.outlier_returns)}")
            for ts, ret in self.outlier_returns[:5]:
                lines.append(f"    {ts}: {ret:+.1%}")
        if self.misaligned_bars:
            lines.append(f"  misaligned bars: {len(self.misaligned_bars)}")
        if self.incomplete_last_bar:
            lines.append("  last bar looks incomplete (close_time is in the future)")
        return "\n".join(lines)


def find_duplicates(df: pd.DataFrame) -> list[pd.Timestamp]:
    dupe_mask = df["open_time"].duplicated(keep=False)
    return sorted(df.loc[dupe_mask, "open_time"].unique())


def find_zero_volume(df: pd.DataFrame) -> list[pd.Timestamp]:
    return list(df.loc[df["volume"] == 0, "open_time"])


def find_misaligned_bars(df: pd.DataFrame, timeframe: Timeframe) -> list[pd.Timestamp]:
    """Bars whose ``open_time`` doesn't fall on the expected grid.

    E.g. a 4h bar must open at 00:00/04:00/08:00/... UTC; a 1h bar at
    the top of the hour; a 1d bar at 00:00 UTC.
    """
    step_seconds = float(timeframe.timedelta_seconds)
    epoch = pd.Timestamp("1970-01-01", tz="UTC")
    offset_seconds = (df["open_time"] - epoch).dt.total_seconds() % step_seconds
    return list(df.loc[offset_seconds != 0.0, "open_time"])


def find_gaps(
    df: pd.DataFrame, expected_open_times: pd.DatetimeIndex
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Contiguous runs of expected bars that are missing from ``df``.

    Returns ``(first_missing, last_missing)`` per run, not one entry per
    missing bar, so a month-long known gap prints as one line.
    """
    present = pd.DatetimeIndex(df["open_time"]).sort_values()
    missing = expected_open_times.difference(present).sort_values()
    if missing.empty:
        return []

    runs: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    run_start = missing[0]
    run_prev = missing[0]
    inferred_step = (
        expected_open_times[1] - expected_open_times[0] if len(expected_open_times) > 1 else None
    )
    for ts in missing[1:]:
        if inferred_step is not None and ts - run_prev == inferred_step:
            run_prev = ts
            continue
        runs.append((run_start, run_prev))
        run_start = ts
        run_prev = ts
    runs.append((run_start, run_prev))
    return runs


def find_outlier_returns(
    df: pd.DataFrame,
    timeframe: Timeframe,
    z_threshold: float = 8.0,
    window_days: float = 30.0,
) -> list[tuple[pd.Timestamp, float]]:
    """Bar-to-bar log-return outliers, flagged by a *rolling* robust
    (median/MAD) z-score.

    8 "standard deviations" is deliberately generous: crypto and
    equities both have fat-tailed returns, and this check exists to
    catch data errors (a misplaced decimal, a stock-split not reflected
    in raw prices) rather than ordinary volatility.

    Uses the median and median-absolute-deviation, not mean/std: a
    single genuine spike drags the mean and inflates the std enough to
    mask itself (the very thing a plain z-score is supposed to catch).
    MAD, scaled by 1.4826 to be comparable to a standard deviation
    under normality, isn't dominated by the outlier(s) it's being used
    to find.

    The window is also rolling, not global, confirmed necessary by
    running this against the real 2019-2026 BTC/ETH backfill in Phase
    0: a single MAD over 7 years of crypto is miscalibrated by
    construction — volatility clusters by regime, so an ordinary move
    during a volatile stretch and an ordinary move during a calm one
    get judged against the same "typical" scale, and the calm-era
    baseline flagged hundreds of perfectly normal 2019 hourly moves as
    "outliers." A trailing `window_days`-day window (30 days, matching
    the ~35-day EWMA vol span ``TrendVolTarget`` uses in spec Section
    5) keeps the baseline local instead.
    """
    if len(df) < 3:
        return []
    sorted_df = df.sort_values("open_time")
    price_ratio: pd.Series = sorted_df["close"] / sorted_df["close"].shift(1)
    log_returns: pd.Series = np.log(price_ratio)

    window_bars = max(20, round(window_days * 86400 / timeframe.timedelta_seconds))
    min_periods = min(int(log_returns.notna().sum()), max(10, window_bars // 4))
    deviation_from_median = (
        log_returns
        - log_returns.rolling(window_bars, min_periods=min_periods, center=True).median()
    )
    rolling_mad = (
        deviation_from_median.abs()
        .rolling(window_bars, min_periods=min_periods, center=True)
        .median()
    )
    robust_std = (rolling_mad * 1.4826).replace(0.0, np.nan)

    z_scores: pd.Series = deviation_from_median / robust_std
    outlier_mask = (z_scores.abs() > z_threshold).fillna(False)
    return list(
        zip(
            sorted_df.loc[outlier_mask, "open_time"],
            log_returns[outlier_mask],
            strict=True,
        )
    )


def detect_incomplete_last_bar(df: pd.DataFrame, now: datetime | None = None) -> bool:
    """True if the last bar's ``close_time`` is in the future relative to ``now``.

    That shape only happens if a still-forming candle slipped into a
    dataset meant to hold only closed bars (design rule 3).
    """
    if df.empty:
        return False
    now = now or datetime.now(UTC)
    last_close = pd.Timestamp(df["close_time"].max())
    return bool(last_close > pd.Timestamp(now))


def find_missing_sessions(
    df: pd.DataFrame, session_dates: pd.DatetimeIndex
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Expected NYSE session dates with no bar in ``df``, compared by
    calendar date rather than exact timestamp.

    We don't yet know (no real Alpaca data fetched) whether a daily
    bar's timestamp is stamped at session open, close or UTC midnight —
    see ``data.calendars.nyse_session_dates`` — so this normalizes both
    sides to a date before comparing, instead of reusing ``find_gaps``'s
    exact-timestamp grid matching.
    """
    present_dates = pd.DatetimeIndex(df["open_time"]).normalize().unique()
    missing = session_dates.difference(present_dates).sort_values()
    return [(d, d) for d in missing]


def validate_equity_series(
    df: pd.DataFrame,
    venue: str,
    symbol: str,
    timeframe: Timeframe,
    session_dates: pd.DatetimeIndex | None = None,
    now: datetime | None = None,
) -> ValidationReport:
    """Like :func:`validate_series`, but for daily equity bars: gaps are
    found by calendar date against the NYSE calendar, and the
    fixed-grid ``find_misaligned_bars`` check is skipped (a daily bar's
    time-of-day convention isn't pinned down yet — see
    ``find_missing_sessions``).
    """
    gaps = find_missing_sessions(df, session_dates) if session_dates is not None else []
    return ValidationReport(
        venue=venue,
        symbol=symbol,
        timeframe=timeframe.value,
        n_bars=len(df),
        duplicate_open_times=find_duplicates(df),
        gaps=gaps,
        zero_volume_bars=find_zero_volume(df),
        outlier_returns=find_outlier_returns(df, timeframe),
        misaligned_bars=[],
        incomplete_last_bar=detect_incomplete_last_bar(df, now=now),
    )


def validate_series(
    df: pd.DataFrame,
    venue: str,
    symbol: str,
    timeframe: Timeframe,
    expected_open_times: pd.DatetimeIndex | None = None,
    now: datetime | None = None,
) -> ValidationReport:
    """Run every check and assemble one report for a single (venue, symbol, timeframe) series."""
    gaps = find_gaps(df, expected_open_times) if expected_open_times is not None else []
    return ValidationReport(
        venue=venue,
        symbol=symbol,
        timeframe=timeframe.value,
        n_bars=len(df),
        duplicate_open_times=find_duplicates(df),
        gaps=gaps,
        zero_volume_bars=find_zero_volume(df),
        outlier_returns=find_outlier_returns(df, timeframe),
        misaligned_bars=find_misaligned_bars(df, timeframe),
        incomplete_last_bar=detect_incomplete_last_bar(df, now=now),
    )
