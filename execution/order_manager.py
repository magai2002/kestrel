"""Order manager (spec Section 4.1's "OMS" box): target quantities vs.
held -> proposed orders.

Trades the **difference** from current holdings (design rule 1) — not
a remembered intent. This is exactly what makes crashes, partial fills
and restarts self-healing: there's no pending "I meant to buy 2 BTC and
only got 0.3 so far" state to recover. Every bar, the order manager
just asks "where are we" vs. "where the latest target says we should
be" and proposes the gap, recomputed fresh — so a partial fill simply
shrinks the gap the next proposal has to close, with no special-casing.

Client order IDs are deterministic (design rule 6):
``{sleeve}-{strategy}-{bar_close_ts}-{symbol}-{seq}``. Restart recovery
("look up open/recent orders by client ID before sending anything") is
a live-mode concern — it needs a broker and persisted state to look
things up against, neither of which exists until Phase 4's live
runner. The ID scheme is already restart-safe; the lookup logic is not
built yet because nothing would exercise it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from core.order import Order, OrderSide, make_client_order_id


def propose_order(
    instrument_id: str,
    symbol: str,
    target_quantity: Decimal,
    held_quantity: Decimal,
    sleeve: str,
    strategy: str,
    bar_close_ts: datetime,
    seq: int = 0,
) -> Order | None:
    """None if ``target_quantity == held_quantity`` already."""
    delta = target_quantity - held_quantity
    if delta == 0:
        return None
    side = OrderSide.BUY if delta > 0 else OrderSide.SELL
    client_order_id = make_client_order_id(sleeve, strategy, bar_close_ts, symbol, seq)
    return Order(
        client_order_id=client_order_id,
        instrument_id=instrument_id,
        side=side,
        quantity=abs(delta),
        created_at=bar_close_ts,
    )


def propose_orders(
    target_quantities: dict[str, Decimal],
    held_quantities: dict[str, Decimal],
    symbols: dict[str, str],
    sleeve: str,
    strategy: str,
    bar_close_ts: datetime,
) -> list[Order]:
    """One proposed order per instrument whose target differs from
    what's held; instruments already at target produce nothing."""
    orders = []
    for instrument_id, target in target_quantities.items():
        held = held_quantities.get(instrument_id, Decimal(0))
        order = propose_order(
            instrument_id,
            symbols[instrument_id],
            target,
            held,
            sleeve,
            strategy,
            bar_close_ts,
            seq=0,
        )
        if order is not None:
            orders.append(order)
    return orders
