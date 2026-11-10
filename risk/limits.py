"""Configurable risk limits (spec Section 6.3).

Defaults are a conservative placeholder, not sleeve-specific: the spec
gives different per-instrument caps for crypto (60%) and equities (30%)
and different price bands (2%/1%) — each sleeve's config constructs its
own ``RiskLimits`` with the right numbers rather than this module
guessing which sleeve is asking.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class GroupCap(BaseModel):
    """E.g. the five equity-index ETFs together capped at 60% (spec
    example) because they move together."""

    model_config = ConfigDict(frozen=True)

    name: str
    instrument_ids: tuple[str, ...]
    cap: float


class RiskLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    per_instrument_weight_cap: float = 0.30
    group_caps: tuple[GroupCap, ...] = ()
    gross_exposure_cap: float = 1.00  # no leverage (design rule: spot and long/flat only)
    max_order_notional_pct: float = 0.30  # fat-finger guard, fraction of sleeve equity
    price_band_pct: float = 0.02
    daily_loss_limit_pct: float = 0.03
    drawdown_limit_pct: float = 0.20
