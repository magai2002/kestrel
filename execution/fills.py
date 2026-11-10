"""Fill models (spec Section 6.2).

Market/marketable orders fill at the next bar's open, price-adjusted
for half the spread and volatility-scaled slippage — always adverse to
the trader (buys pay up, sells receive less). Passive limit orders only
fill if the next bar actually trades *through* the limit price (spec:
"not merely touches it"). Both proportion partial fills to bar volume
under a participation cap.

**Only the market path is wired into Phase 2's order manager.** Passive
limits and the live path's price-protected marketable limits with
timeout/cancel/re-quote (spec Section 12, Phase 4) need a live venue's
order-ack/reject behavior to mean anything; a backtest fills everything
it can, immediately, against bars that already happened. Both fill
functions are built and tested now anyway, since spec Section 6.2
describes them as one fill model, not phase-gated — Phase 4 reuses
these, it doesn't reimplement them.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from core.fill import Fill
from core.order import Order, OrderSide
from execution.costs import (
    CostModel,
    belgian_tob,
    commission,
    half_spread_price_adjustment,
    slippage_price_adjustment,
)


def market_fill_price(
    open_price: Decimal, side: OrderSide, bar_volatility: Decimal, cost_model: CostModel
) -> Decimal:
    adjustment = half_spread_price_adjustment(
        open_price, cost_model.spread_bps
    ) + slippage_price_adjustment(open_price, bar_volatility, cost_model.slippage_k)
    return open_price + adjustment if side == OrderSide.BUY else open_price - adjustment


def capped_fill_quantity(
    order_quantity: Decimal, bar_volume: Decimal, participation_cap: Decimal
) -> Decimal:
    """The rest of ``order_quantity`` beyond the participation cap is
    simply not filled this bar — the diff-based order manager (design
    rule 1) naturally re-proposes the shortfall next bar, since it
    always trades (target - held), not a remembered pending amount."""
    max_fillable = bar_volume * participation_cap
    return min(order_quantity, max_fillable)


def trades_through_limit(
    side: OrderSide, limit_price: Decimal, bar_high: Decimal, bar_low: Decimal
) -> bool:
    """True only if the bar's range strictly crosses the limit price —
    merely touching it (bar_low == limit for a buy) doesn't count."""
    if side == OrderSide.BUY:
        return bar_low < limit_price
    return bar_high > limit_price


def build_market_fill(
    order: Order,
    open_price: Decimal,
    bar_volume: Decimal,
    bar_volatility: Decimal,
    cost_model: CostModel,
    participation_cap: Decimal,
    timestamp: datetime,
) -> Fill | None:
    """None if nothing fills this bar (e.g. zero volume)."""
    fill_quantity = capped_fill_quantity(order.quantity, bar_volume, participation_cap)
    if fill_quantity <= 0:
        return None
    fill_price = market_fill_price(open_price, order.side, bar_volatility, cost_model)
    notional = fill_price * fill_quantity
    fee = commission(notional, cost_model.fee_pct) + belgian_tob(
        notional, cost_model.tob_pct, cost_model.tob_cap
    )
    return Fill(
        client_order_id=order.client_order_id,
        instrument_id=order.instrument_id,
        side=order.side,
        quantity=fill_quantity,
        price=fill_price,
        fee=fee,
        timestamp=timestamp,
    )


def build_passive_limit_fill(
    order: Order,
    limit_price: Decimal,
    bar_high: Decimal,
    bar_low: Decimal,
    bar_volume: Decimal,
    cost_model: CostModel,
    participation_cap: Decimal,
    timestamp: datetime,
) -> Fill | None:
    """None if the bar never traded through the limit, or nothing fills."""
    if not trades_through_limit(order.side, limit_price, bar_high, bar_low):
        return None
    fill_quantity = capped_fill_quantity(order.quantity, bar_volume, participation_cap)
    if fill_quantity <= 0:
        return None
    notional = limit_price * fill_quantity
    fee = commission(notional, cost_model.fee_pct) + belgian_tob(
        notional, cost_model.tob_pct, cost_model.tob_cap
    )
    return Fill(
        client_order_id=order.client_order_id,
        instrument_id=order.instrument_id,
        side=order.side,
        quantity=fill_quantity,
        price=limit_price,
        fee=fee,
        timestamp=timestamp,
    )
