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
