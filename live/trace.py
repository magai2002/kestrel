"""The per-cycle **decision trace** behind the dashboard's Decisions page.

One ``decision`` event per cycle that reaches the decision step (and a
risk-only one for halted/paused cycles), describing every stage of
``portfolio.rebalance.plan_rebalance`` for every asset:

    strategy internals -> raw target -> constraints -> buffer band
    -> fit-to-limits -> order -> pre-trade risk

plus how far each kill switch is from firing.

**Shareable numbers only** (the public API returns this payload): weights,
forecasts, volatilities, percentages, per-unit prices and order sizes *as
a percent of sleeve equity*. Never cash, quantities, notionals or fees in
currency. ``tests/unit/api/test_api.py`` runs a real cycle through the API
and greps for leaks.

Nothing here feeds back into trading: it only *reports* the values
``plan_rebalance`` and the strategy produced, and a failure building it is
logged and recorded, never raised into the cycle.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from portfolio.rebalance import RebalancePlan
from portfolio.sizing import should_trade
from strategies.base import StrategyContext
from strategies.overlay import explain_strategy

if TYPE_CHECKING:
    from live.sleeve_runner import SleeveRuntime
    from live.state import SleeveState

log = structlog.get_logger(__name__)
EPS = 1e-9


def risk_snapshot(
    rt: SleeveRuntime,
    state: SleeveState,
    equity: float,
    weights_now: dict[str, float],
    bar_close: datetime,
    now: datetime,
    after: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Each limit next to its current value (and, after a decision, the
    value the decision aims for)."""
    risk = rt.sleeve.risk
    day_open = state.day_open_equity or equity
    peak = state.equity_peak or equity
    groups = []
    for g in risk.group_caps:
        members = [a for a in g.assets if a in weights_now]
        groups.append(
            {
                "name": g.name,
                "members": members,
                "value": sum(weights_now[a] for a in members),
                "after": None if after is None else sum(after.get(a, 0.0) for a in members),
                "cap": g.cap,
            }
        )
    bar_s = rt.sleeve.decision_bar.timedelta_seconds
    calendar = rt.listing(rt.sleeve.asset_ids[0]).calendar
    return {
        "daily_pnl": {
            "value": equity / day_open - 1.0 if day_open else 0.0,
            "limit": -risk.daily_loss_limit_pct,
            "absolute_limit_set": rt.daily_loss_limit_abs is not None,
        },
        "drawdown": {
            "value": equity / peak - 1.0 if peak else 0.0,
            "limit": -risk.drawdown_limit_pct,
        },
        "gross": {
            "value": sum(weights_now.values()),
            "after": None if after is None else sum(after.values()),
            "cap": risk.gross_exposure_cap,
        },
        "instrument_cap": risk.per_instrument_weight_cap,
        "groups": groups,
        "data": {
            "bar_close": bar_close.isoformat(),
            "age_seconds": (now - bar_close).total_seconds(),
            "stale_after": f"{risk.stale_data_bars:g} "
            + (f"{calendar} sessions" if calendar else f"bars ({bar_s // 3600:g}h each)"),
            "stale_after_seconds": None if calendar else risk.stale_data_bars * bar_s,
        },
        "errors": {
            "recent": len(state.error_times),
            "limit": risk.error_burst_count,
            "window_minutes": risk.error_burst_window_minutes,
        },
        "max_order_pct": risk.max_order_notional_pct,
        "price_band_pct": risk.price_band_pct,
    }


def _constraint_reason(
    rt: SleeveRuntime, aid: str, raw: dict[str, float], got: float
) -> str | None:
    """Which step of ``portfolio.constraints.constrain_weights`` bound this
    asset (labels only; the numbers come from the real call)."""
    want = raw.get(aid, 0.0)
    if got >= want - EPS:
        return None
    risk = rt.sleeve.risk
    clipped = {a: min(max(w, 0.0), risk.per_instrument_weight_cap) for a, w in raw.items()}
    if clipped[aid] < want - EPS and got >= clipped[aid] - EPS:
        return f"instrument cap {risk.per_instrument_weight_cap:.0%}"
    for g in risk.group_caps:
        if aid in g.assets and sum(clipped.get(a, 0.0) for a in g.assets) > g.cap:
            return f"group cap {g.name} {g.cap:.0%}"
    return f"gross cap {risk.gross_exposure_cap:.0%}"


def decision_trace(
    rt: SleeveRuntime,
    ctx: StrategyContext,
    raw: dict[str, float],
    plan: RebalancePlan,
    weights_now: dict[str, float],
    last_prices: dict[str, Decimal],
    sizing_equity: Decimal,
) -> dict[str, Any]:
    try:
        strategies = explain_strategy(rt.strategy, rt.strategy_label, ctx)
    except Exception as exc:  # reporting must never stop a cycle
        log.warning("decision_trace_explain_failed", sleeve=rt.name, error=str(exc))
        strategies = [{"strategy": rt.strategy_label, "error": str(exc)}]

    orders: dict[str, dict[str, Any]] = {}
    for order, decision in plan.decisions:
        aid = order.instrument_id
        size = (
            float(order.quantity * last_prices[aid] / sizing_equity) if sizing_equity > 0 else None
        )
        orders[aid] = {
            "side": order.side.value,
            "size_pct_of_equity": size,
            "approved": decision.approved,
            "reason": decision.reason,
        }

    buffer = rt.sleeve.buffer_pct
    assets: dict[str, Any] = {}
    for aid in rt.sleeve.asset_ids:
        target = plan.target_weights.get(aid, 0.0)
        current = weights_now.get(aid, 0.0)
        trades = should_trade(current, target, buffer)
        aimed = target if trades else current
        buffered = plan.buffered_weights.get(aid, current)
        trimmed = buffered < aimed - EPS
        sent = orders.get(aid)
        if sent is not None:
            action = "trim to fit limits" if trimmed and not trades else sent["side"]
        elif trades:
            action = "no order (rounds to the held quantity)"
        else:
            action = "hold (inside buffer)"
        assets[aid] = {
            "price": float(last_prices[aid]),
            "by_strategy": {
                s["strategy"]: s.get("allocation", 1.0) * s.get("weights", {}).get(aid, 0.0)
                for s in strategies
                if "weights" in s
            },
            "raw": raw.get(aid, 0.0),
            "constrained": target,
            "constraint": _constraint_reason(rt, aid, raw, target),
            "current": current,
            "gap": target - current,
            "outside_buffer": trades,
            "buffered": buffered,
            "trimmed_to_fit_limits": trimmed,
            "action": action,
            "order": sent,
        }
    return {
        "strategy_label": rt.strategy_label,
        "buffer_pct": buffer,
        "strategies": strategies,
        "assets": assets,
        "n_orders": len(plan.decisions),
        "n_approved": len(plan.approved),
    }
