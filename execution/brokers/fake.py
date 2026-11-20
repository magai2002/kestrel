"""An in-memory exchange implementing ``LiveBroker``, with fault
injection. Used by the order-manager tests, the broker contract suite
and the chaos scenarios (``tests/chaos/``): "the engine dies between
send and ack", "the venue returns 5xx but did take the order", "only a
third of the order fills before the timeout".

Matching model: a buy limit at or above ``market_price`` fills (a sell
at or below), up to ``fill_fraction`` of the remaining quantity per
``tick()``; anything else rests until cancelled.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal

from core.order import OrderSide
from execution.brokers.base import (
    BrokerError,
    BrokerFill,
    BrokerOrder,
    BrokerOrderStatus,
    TransientBrokerError,
)


@dataclass
class Faults:
    """Each counter fires on the next N matching calls, then stops."""

    place_lost_ack: int = 0  # order IS accepted, but the call raises Transient
    place_unreachable: int = 0  # order NOT accepted, call raises Transient
    place_reject: int = 0  # definite rejection
    get_transient: int = 0
    crash_after_place: int = 0  # raise SystemExit right after accepting (engine killed)


@dataclass
class FakeVenue:
    market_price: dict[str, Decimal]
    balances: dict[str, Decimal] = field(default_factory=dict)  # symbol -> base qty, plus quote ccy
    quote_currency: str = "USDT"
    fee_rate: Decimal = Decimal("0.001")
    fill_fraction: Decimal = Decimal(1)
    fill_on_place: bool = True
    faults: Faults = field(default_factory=Faults)
    now: datetime = field(default_factory=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    name: str = "fake"
    orders: dict[str, BrokerOrder] = field(default_factory=dict)
    placed_count: int = 0
    _seq: int = 0

    # --- simulation controls --------------------------------------------
    def tick(self) -> None:
        """Advance matching one step for every open order."""
        for cid, order in list(self.orders.items()):
            if order.status is BrokerOrderStatus.OPEN:
                self.orders[cid] = self._match(order)

    def _match(self, order: BrokerOrder) -> BrokerOrder:
        px = self.market_price[order.symbol]
        assert order.limit_price is not None
        crosses = (order.side is OrderSide.BUY and order.limit_price >= px) or (
            order.side is OrderSide.SELL and order.limit_price <= px
        )
        if not crosses or order.remaining <= 0:
            return order
        qty = order.remaining if self.fill_fraction >= 1 else (order.remaining * self.fill_fraction)
        qty = min(order.remaining, qty.quantize(Decimal("0.00000001")))
        if qty <= 0:
            return order
        self._seq += 1
        fee = qty * px * self.fee_rate
        fill = BrokerFill(
            fill_id=f"t{self._seq}",
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=qty,
            price=px,
            fee=fee,
            timestamp=self.now,
        )
        sign = 1 if order.side is OrderSide.BUY else -1
        self.balances[order.symbol] = self.balances.get(order.symbol, Decimal(0)) + sign * qty
        q = self.quote_currency
        self.balances[q] = self.balances.get(q, Decimal(0)) - sign * qty * px - fee
        filled = order.filled_quantity + qty
        prev_notional = (order.avg_fill_price or Decimal(0)) * order.filled_quantity
        avg = (prev_notional + qty * px) / filled
        status = BrokerOrderStatus.FILLED if filled >= order.quantity else BrokerOrderStatus.OPEN
        return replace(
            order,
            filled_quantity=filled,
            avg_fill_price=avg,
            status=status,
            fills=(*order.fills, fill),
        )

    # --- LiveBroker ------------------------------------------------------
    def server_time(self) -> datetime:
        return self.now

    def place_limit_order(
        self,
        client_order_id: str,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        limit_price: Decimal,
    ) -> BrokerOrder:
        if self.faults.place_unreachable:
            self.faults.place_unreachable -= 1
            raise TransientBrokerError("fake: connection reset before the venue saw the order")
        if self.faults.place_reject:
            self.faults.place_reject -= 1
            raise BrokerError("fake: insufficient balance")
        if client_order_id in self.orders:
            # Real venues reject a duplicate client id; this is what makes
            # "look up before retrying" safe and necessary.
            raise BrokerError(f"fake: duplicate client order id {client_order_id}")
        self.placed_count += 1
        order = BrokerOrder(
            client_order_id=client_order_id,
            broker_order_id=f"o{self.placed_count}",
            symbol=symbol,
            side=side,
            quantity=quantity,
            limit_price=limit_price,
            status=BrokerOrderStatus.OPEN,
            filled_quantity=Decimal(0),
            avg_fill_price=None,
        )
        if self.fill_on_place:
            order = self._match(order)
        self.orders[client_order_id] = order
        if self.faults.crash_after_place:
            self.faults.crash_after_place -= 1
            raise SystemExit("fake: engine killed between send and ack")
        if self.faults.place_lost_ack:
            self.faults.place_lost_ack -= 1
            raise TransientBrokerError("fake: 503 after accepting the order")
        return order

    def get_order(self, client_order_id: str, symbol: str) -> BrokerOrder | None:
        if self.faults.get_transient:
            self.faults.get_transient -= 1
            raise TransientBrokerError("fake: timeout")
        return self.orders.get(client_order_id)

    def cancel_order(self, client_order_id: str, symbol: str) -> BrokerOrder | None:
        order = self.orders.get(client_order_id)
        if order is None or order.status.is_final:
            return order
        order = replace(order, status=BrokerOrderStatus.CANCELLED)
        self.orders[client_order_id] = order
        return order

    def open_orders(self, symbols: list[str]) -> list[BrokerOrder]:
        return [
            o
            for o in self.orders.values()
            if o.status is BrokerOrderStatus.OPEN and o.symbol in symbols
        ]

    def positions(self, symbols: list[str]) -> dict[str, Decimal]:
        return {s: self.balances.get(s, Decimal(0)) for s in symbols}

    def cash(self, currency: str) -> Decimal:
        return self.balances.get(currency, Decimal(0))

    def last_price(self, symbol: str) -> Decimal | None:
        return self.market_price.get(symbol)
