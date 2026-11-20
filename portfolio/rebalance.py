"""One rebalancing decision: target weights -> buffered target quantities
-> proposed orders -> pre-trade risk. The single function the backtest
engine *and* the live runner call, so "strategy, portfolio, risk and
order-manager code is identical across all four modes" (spec Section
3.2) is true by construction, not by two copies kept in sync.

Inputs are plain values (weights, quantities, prices, limits); nothing
here knows whether it's running in a backtest or against a broker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from core.order import Order
from execution.order_manager import propose_orders
from portfolio.constraints import constrain_weights
from portfolio.construction import implied_weight, target_quantity
from portfolio.sizing import should_trade
from risk.limits import RiskLimits
from risk.pre_trade import RiskDecision, evaluate_order

# Half a percentage point of sleeve equity: below this, a drifted
# position over its cap is left alone (a buy that would worsen it is
# still rejected by risk); above it, it's trimmed regardless of the buffer.
FORCE_TRIM_TOLERANCE = 0.005


def _absorb(weights: dict[str, float], members: list[str], excess: float) -> float:
    """Scale ``members`` down proportionally to remove up to ``excess``;
    returns what's left over (> 0 only if they all hit zero)."""
    total = sum(weights[m] for m in members)
    if total <= 0 or excess <= 0:
        return max(excess, 0.0)
    take = min(excess, total)
    scale = 1.0 - take / total
    for m in members:
        weights[m] *= scale
    return excess - take


def fit_to_limits(
    weights: dict[str, float], trading: set[str], limits: RiskLimits
) -> dict[str, float]:
    """Make the *buffered* weight vector — constrained targets for assets
    being traded, drifted current weights for the rest — satisfy every
    exposure limit.

    Why this step exists (found on the real 10-ETF equities backtest in
    Phase 6, 224 of 311 orders rejected): constraining only the strategy's
    *targets* isn't enough. Buffering leaves untraded positions at their
    drifted weights, which can push an instrument, a group or the gross
    total over its cap. The pre-trade check then rejected every buy in the
    group, every bar, forever.

    The rule, in order, for each instrument cap, then each group, then
    gross:

    1. **Assets being traded absorb the excess first** (buys get sized
       down). No extra orders: they were trading anyway.
    2. **Only a material leftover** (> ``FORCE_TRIM_TOLERANCE``) trims
       untraded positions too, which forces a risk-reducing sell inside
       the buffer. Below it, the drift is left alone: forcing tiny trims
       produced a stream of sub-minimum-notional orders (seen on the
       golden run, where fees push a fully invested position ~0.4% over
       100% gross).

    Every step only scales weights down, so a later step never re-breaks
    an earlier one.
    """
    w = dict(weights)
    margin = 1.0 - 1e-9

    def apply(members: list[str], cap: float) -> None:
        excess = sum(w[m] for m in members) - cap * margin
        if excess <= 1e-12:
            return
        movable = [m for m in members if m in trading]
        left = _absorb(w, movable, excess)
        if left > FORCE_TRIM_TOLERANCE:
            _absorb(w, [m for m in members if m not in trading], left)

    for aid in list(w):
        apply([aid], limits.per_instrument_weight_cap)
    for group in limits.group_caps:
        apply([m for m in group.instrument_ids if m in w], group.cap)
    apply(list(w), limits.gross_exposure_cap)
    return w


@dataclass(frozen=True)
class RebalancePlan:
    target_weights: dict[str, float]  # after constraints
    buffered_weights: dict[str, float]  # what we actually aim to hold
    target_quantities: dict[str, Decimal]
    approved: list[Order]
    decisions: list[tuple[Order, RiskDecision]] = field(default_factory=list)

    @property
    def rejected(self) -> list[tuple[Order, RiskDecision]]:
        return [(o, d) for o, d in self.decisions if not d.approved]


def plan_rebalance(
    raw_target_weights: dict[str, float],
    held_quantities: dict[str, Decimal],
    last_prices: dict[str, Decimal],
    sleeve_equity: Decimal,
    symbols: dict[str, str],
    step_sizes: dict[str, Decimal],
    min_notionals: dict[str, Decimal],
    risk_limits: RiskLimits,
    buffer_pct: float,
    sleeve: str,
    strategy: str,
    bar_close_ts: datetime,
    reference_prices: dict[str, Decimal] | None = None,
) -> RebalancePlan:
    """``reference_prices`` is each order's limit price where the live
    path sends a marketable limit (checked against the price band);
    a backtest's market order uses the last price, i.e. omit it."""
    target_weights = constrain_weights(raw_target_weights, risk_limits)
    buffered: dict[str, float] = {}
    targets: dict[str, Decimal] = {}
    for aid in held_quantities:
        held = held_quantities[aid]
        current = implied_weight(held, last_prices[aid], sleeve_equity)
        raw = target_weights.get(aid, 0.0)
        if should_trade(current, raw, buffer_pct):
            buffered[aid] = raw
            targets[aid] = target_quantity(raw, sleeve_equity, last_prices[aid], step_sizes[aid])
        else:
            # Not trading: keep the EXACT held quantity, never one
            # reconstructed from the weight (quantity -> weight ->
            # quantity isn't idempotent after Decimal rounding; see
            # CLAUDE.md). Found on real BTC/ETH data in Phase 2.
            buffered[aid] = current
            targets[aid] = held

    trading = {aid for aid in held_quantities if targets[aid] != held_quantities[aid]}
    fitted = fit_to_limits(buffered, trading, risk_limits)
    for aid, w in fitted.items():
        if w < buffered[aid] - 1e-12:
            buffered[aid] = w
            targets[aid] = target_quantity(w, sleeve_equity, last_prices[aid], step_sizes[aid])

    proposed = propose_orders(targets, held_quantities, symbols, sleeve, strategy, bar_close_ts)
    approved: list[Order] = []
    decisions: list[tuple[Order, RiskDecision]] = []
    for order in proposed:
        aid = order.instrument_id
        ref = (reference_prices or {}).get(aid, last_prices[aid])
        decision = evaluate_order(
            order=order,
            reference_price=ref,
            step_size=step_sizes[aid],
            min_notional=min_notionals[aid],
            proposed_weights=buffered,
            sleeve_equity=sleeve_equity,
            last_trade_price=last_prices[aid],
            limits=risk_limits,
        )
        decisions.append((order, decision))
        if decision.approved:
            approved.append(order)
    return RebalancePlan(target_weights, buffered, targets, approved, decisions)
