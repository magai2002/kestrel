"""Kill switches (spec Section 6.3): trip -> cancel open orders ->
alert -> **stay tripped until reset by hand**. There's no hand in the
backtest path, so a tripped switch here simply blocks new positions
for the rest of the run — Phase 4's admin CLI adds the actual reset
mechanism once there's a human (and a live venue) in the loop.

Only the two checks computable from a sleeve's own equity curve are
implemented here: daily loss and drawdown from peak. Stale data,
reconciliation mismatch, error burst and clock-drift kill switches are
live-venue concepts (same spec section) and arrive with the live
runner in Phase 4 — there's no venue, no reconciler and no clock drift
to speak of in a backtest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class KillSwitchState:
    tripped: bool = False
    reason: str | None = None
    tripped_at: datetime | None = None


def check_daily_loss(equity_today_open: float, equity_now: float, limit_pct: float) -> str | None:
    """A trip reason, or None if within the limit."""
    if equity_today_open <= 0:
        return None
    loss = (equity_now - equity_today_open) / equity_today_open
    if loss < -limit_pct:
        return f"daily loss {loss:.1%} exceeds limit {limit_pct:.1%}"
    return None


def check_drawdown(equity_peak: float, equity_now: float, limit_pct: float) -> str | None:
    if equity_peak <= 0:
        return None
    drawdown = (equity_now - equity_peak) / equity_peak
    if drawdown < -limit_pct:
        return f"drawdown {drawdown:.1%} exceeds limit {limit_pct:.1%}"
    return None


def evaluate(
    state: KillSwitchState,
    equity_today_open: float,
    equity_peak: float,
    equity_now: float,
    now: datetime,
    daily_loss_limit_pct: float,
    drawdown_limit_pct: float,
) -> KillSwitchState:
    """Once tripped, stays tripped: this never un-trips a switch just
    because equity recovered — that's a hand-reset action, not an
    automatic one (spec Section 6.3)."""
    if state.tripped:
        return state
    reason = check_daily_loss(
        equity_today_open, equity_now, daily_loss_limit_pct
    ) or check_drawdown(equity_peak, equity_now, drawdown_limit_pct)
    if reason is not None:
        return KillSwitchState(tripped=True, reason=reason, tripped_at=now)
    return state


# --- live-only kill switches (Phase 4) ---------------------------------
# Each is a pure check returning a trip reason or None. Persisting the
# tripped state, cancelling orders and alerting is the live runner's job
# (``live/sleeve_runner.py``); "stay tripped until reset by hand" is
# enforced there, via ``kestrel admin reset-kill-switch``.

KILL_SWITCH_NAMES = (
    "daily_loss",
    "drawdown",
    "stale_data",
    "reconcile_mismatch",
    "error_burst",
    "clock_drift",
)


def check_stale_data(
    last_bar_close: datetime | None, now: datetime, bar_seconds: float, max_bars: float
) -> str | None:
    """Trip if the newest *closed* bar is older than ``max_bars`` bar
    intervals (spec: 2x). For a daily sleeve on an exchange calendar the
    caller passes the gap measured in *sessions*, not wall-clock days, so
    a weekend isn't "stale" — see ``live/sleeve_runner.py``."""
    if last_bar_close is None:
        return "no closed bar available at all"
    age = (now - last_bar_close).total_seconds()
    if age > bar_seconds * max_bars:
        return f"latest closed bar is {age / 3600:.1f}h old (> {max_bars:g} bars of {bar_seconds / 3600:g}h)"
    return None


def check_reconcile(mismatches: dict[str, str]) -> str | None:
    if mismatches:
        return "reconciliation mismatch: " + "; ".join(
            f"{k}: {v}" for k, v in sorted(mismatches.items())
        )
    return None


def check_error_burst(
    error_times: list[datetime], now: datetime, max_errors: int, window_minutes: float
) -> str | None:
    recent = [t for t in error_times if (now - t).total_seconds() <= window_minutes * 60]
    if len(recent) >= max_errors:
        return (
            f"{len(recent)} broker errors in the last {window_minutes:g} min (limit {max_errors})"
        )
    return None


def check_clock_drift(venue_time: datetime, local_time: datetime, max_seconds: float) -> str | None:
    drift = abs((venue_time - local_time).total_seconds())
    if drift > max_seconds:
        return f"clock drift {drift:.1f}s vs venue (limit {max_seconds:g}s)"
    return None
