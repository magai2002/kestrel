"""Broker protocols (spec Section 3.2 / design rule 1): strategy,
portfolio, risk and order-manager code is identical across all four run
modes — only the clock, data feed and broker adapter change.

Two protocols, because a backtest and a live venue genuinely differ in
shape:

- ``Broker`` — the backtest's: "fill this order against this historical
  bar", synchronous, no state on the venue side.
- ``LiveBroker`` — a real (or paper/testnet, or simulated-live) venue:
  orders are *placed*, then *observed* over time; they can rest,
  partially fill, get rejected, or be cancelled; positions and cash
  exist on the venue side and must be reconciled against our ledger.

Every ``LiveBroker`` adapter passes the same contract suite
(``tests/contract/test_live_broker_contract.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from core.bar import Bar
from core.fill import Fill
from core.order import Order, OrderSide


class Broker(Protocol):
    def submit_order(self, order: Order, bar: Bar, trailing_volatility: Decimal) -> Fill | None:
        """Attempt to fill ``order`` against ``bar``.

        ``trailing_volatility`` is a volatility estimate computed from
        bars *strictly before* ``bar`` — deliberately not derived from
        ``bar`` itself (e.g. its own high-low range), which would let a
        fill priced at this bar's open use information from later in
        the same bar that hadn't happened yet at that moment. The
        engine computes it from history, same as a strategy would.

        Returns ``None`` if nothing filled this bar (e.g. zero volume).
        """
        ...


class BrokerOrderStatus(StrEnum):
    OPEN = "open"  # accepted, resting (possibly partially filled)
    FILLED = "filled"
    CANCELLED = "cancelled"  # by us, or expired; may still be partially filled
    REJECTED = "rejected"

    @property
    def is_final(self) -> bool:
        return self is not BrokerOrderStatus.OPEN


@dataclass(frozen=True)
class BrokerFill:
    """One execution, as the venue reports it. ``fill_id`` is the
    venue's own id (trade id / execId): fills are deduplicated on it, so
    re-reading the same order after a restart never double-counts."""

    fill_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    fee: Decimal  # in the quote currency; adapters convert or flag otherwise
    timestamp: datetime


@dataclass(frozen=True)
class BrokerOrder:
    client_order_id: str  # OUR full deterministic id (design rule 6)
    broker_order_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    limit_price: Decimal | None
    status: BrokerOrderStatus
    filled_quantity: Decimal
    avg_fill_price: Decimal | None
    fills: tuple[BrokerFill, ...] = field(default_factory=tuple)
    reject_reason: str | None = None

    @property
    def remaining(self) -> Decimal:
        return self.quantity - self.filled_quantity


class BrokerError(RuntimeError):
    """A definite failure: the venue said no (bad params, insufficient
    funds...). Safe to treat the request as not having happened."""


class TransientBrokerError(BrokerError):
    """Timeout, 5xx, dropped connection: **the request may or may not
    have reached the venue.** The caller must look the order up by its
    client id before retrying (spec Section 3.3: a Binance 5xx doesn't
    mean the order failed) — retrying blind is how duplicate orders
    happen."""


class LiveBroker(Protocol):
    name: str

    def server_time(self) -> datetime: ...

    def place_limit_order(
        self,
        client_order_id: str,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        limit_price: Decimal,
    ) -> BrokerOrder:
        """Place a limit order tagged with ``client_order_id``. Raises
        ``BrokerError`` (definite) or ``TransientBrokerError`` (unknown)."""
        ...

    def get_order(self, client_order_id: str, symbol: str) -> BrokerOrder | None:
        """The venue's current view of the order, fills included; None
        if the venue has never seen this client id."""
        ...

    def cancel_order(self, client_order_id: str, symbol: str) -> BrokerOrder | None:
        """Cancel if still open; returns the final state (or None if unknown).
        Cancelling an already-final order is not an error."""
        ...

    def open_orders(self, symbols: list[str]) -> list[BrokerOrder]: ...

    def positions(self, symbols: list[str]) -> dict[str, Decimal]:
        """Held quantity per symbol (base units / shares), 0 if none."""
        ...

    def cash(self, currency: str) -> Decimal: ...

    def last_price(self, symbol: str) -> Decimal | None:
        """A current tradeable reference price (last trade or mid) for
        re-quoting; None if unavailable."""
        ...
