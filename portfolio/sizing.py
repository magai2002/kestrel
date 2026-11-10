"""Volatility targeting and position buffering (spec Section 4.2).

``vol_target_weight`` is the shared primitive: given a sleeve's
annualised vol target and an instrument's realized annualised vol, how
big a position (as a fraction of sleeve equity) would hit that target.
``strategies/trend_vol_target.py`` uses this same function — the
formula lives here once, not duplicated between a strategy and the
portfolio layer.

Buffering here differs from ``research/vectorized.py::apply_buffer``
in what it compares against: the vectorized backtester only has a
remembered *previous target* (it never tracks real positions), so it
compares new target vs. last target. The event-driven engine has real
positions, so ``should_trade`` compares the new target against the
weight *actually implied by current holdings* — self-correcting after
any rounding or slippage drift, which comparing against a remembered
target wouldn't be.
"""

from __future__ import annotations

import pandas as pd


def vol_target_weight(
    target_vol_annual: float, realized_vol_annual: float | pd.Series
) -> float | pd.Series:
    """Fraction of sleeve equity that would hit ``target_vol_annual``
    given an instrument's realized annualised volatility.

    Zero (not an error, not infinity) where realized vol is zero,
    negative or NaN — a dead/no-data instrument should size to
    nothing, not blow up into an infinite position.

    Dual-mode on purpose: a scalar for the event-driven engine's
    per-bar sizing, or a whole ``pd.Series`` for
    ``strategies.trend_vol_target``'s vectorized form — one formula,
    not duplicated between the two.
    """
    if isinstance(realized_vol_annual, pd.Series):
        safe = realized_vol_annual.where(realized_vol_annual > 0)
        return (target_vol_annual / safe).fillna(0.0)
    if realized_vol_annual <= 0 or realized_vol_annual != realized_vol_annual:  # NaN check
        return 0.0
    return target_vol_annual / realized_vol_annual


def should_trade(current_weight: float, target_weight: float, buffer_pct: float) -> bool:
    """Position buffering (spec Section 5): only trade if the target
    differs from the weight actually implied by current holdings by
    more than ``buffer_pct``."""
    if buffer_pct <= 0:
        return current_weight != target_weight
    return abs(target_weight - current_weight) > buffer_pct


def buffered_target(current_weight: float, target_weight: float, buffer_pct: float) -> float:
    """The weight to actually act on: the new target if it clears the
    buffer, otherwise the status quo (current weight) — i.e. "don't
    trade" expressed as "the target is wherever we already are."""
    if should_trade(current_weight, target_weight, buffer_pct):
        return target_weight
    return current_weight
