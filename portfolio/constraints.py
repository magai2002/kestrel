"""Make a target-weight vector *satisfy* the sleeve's limits before it
becomes orders: clip per-instrument, scale groups down, scale gross
down — in that order.

Why this exists (found while wiring up the 10-ETF equities sleeve in
Phase 3): pre-trade risk checks (``risk/pre_trade.py``) *reject* an
order whose resulting weights breach a limit. That's the right
behaviour for a last-line guard, but as the only mechanism it's
pathological: a strategy asking for 130% gross exposure doesn't get
"100%", it gets *nothing* — every order rejected, every bar, forever.
The portfolio layer's job is to turn "what the strategy would like"
into "the closest thing the limits allow"; risk then only ever rejects
genuine anomalies (fat fingers, price-band breaches, sub-minimum
notionals).

Every step only ever scales weights *down*, so a later step can never
re-break an earlier one. A tiny safety margin keeps a scaled total from
landing a floating-point hair above its cap and getting rejected anyway.
"""

from __future__ import annotations

import pandas as pd

from risk.limits import RiskLimits

_MARGIN = 1.0 - 1e-9


def constrain_weights(weights: dict[str, float], limits: RiskLimits) -> dict[str, float]:
    out = {k: min(max(float(v), 0.0), limits.per_instrument_weight_cap) for k, v in weights.items()}
    for group in limits.group_caps:
        members = [k for k in group.instrument_ids if k in out]
        total = sum(out[k] for k in members)
        if total > group.cap and total > 0:
            scale = group.cap / total * _MARGIN
            for k in members:
                out[k] *= scale
    gross = sum(out.values())
    if gross > limits.gross_exposure_cap and gross > 0:
        scale = limits.gross_exposure_cap / gross * _MARGIN
        out = {k: v * scale for k, v in out.items()}
    return out


def constrain_weight_frame(weights: pd.DataFrame, limits: RiskLimits) -> pd.DataFrame:
    """Row-wise ``constrain_weights`` for the vectorized backtester."""
    out = weights.clip(lower=0.0, upper=limits.per_instrument_weight_cap)
    for group in limits.group_caps:
        members = [c for c in group.instrument_ids if c in out.columns]
        if not members:
            continue
        total = out[members].sum(axis=1)
        scale = (group.cap / total.where(total > group.cap) * _MARGIN).fillna(1.0)
        out[members] = out[members].mul(scale, axis=0)
    gross = out.sum(axis=1)
    cap = limits.gross_exposure_cap
    scale = (cap / gross.where(gross > cap) * _MARGIN).fillna(1.0)
    return out.mul(scale, axis=0)
