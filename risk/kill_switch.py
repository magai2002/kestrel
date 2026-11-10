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
