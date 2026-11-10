"""Order: what the order manager proposes, and what (once approved by
risk) gets sent to a broker.

**Client order IDs are deterministic** (design rule 6):
``{sleeve}-{strategy}-{bar_close_ts}-{symbol}-{seq}``. On restart, the
order manager looks up open/recent orders by this ID before sending
anything — so a crash between "order sent" and "ack received" can't
turn into a duplicate order on recovery.

Orders are immutable value objects, not mutated in place: a status
change (pending -> submitted -> filled) is a *new* ``Order`` built via
``dataclasses.replace``, which is also exactly the shape the event log
(design rule 7, append-only) wants — each transition is its own event,
not an in-place update that erases what came before it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(StrEnum):
    PENDING = "pending"  # proposed by the order manager, not yet risk-checked
    REJECTED = "rejected"  # a pre-trade risk check failed
    SUBMITTED = "submitted"  # sent to the broker, awaiting a fill
    FILLED = "filled"
    CANCELLED = "cancelled"


def make_client_order_id(
    sleeve: str, strategy: str, bar_close_ts: datetime, symbol: str, seq: int
) -> str:
    if bar_close_ts.tzinfo is None:
        raise ValueError("bar_close_ts must be timezone-aware")
    timestamp = bar_close_ts.strftime("%Y%m%dT%H%M%SZ")
    return f"{sleeve}-{strategy}-{timestamp}-{symbol}-{seq}"


@dataclass(frozen=True)
class Order:
    client_order_id: str
    instrument_id: str
    side: OrderSide
    quantity: Decimal  # always positive; `side` carries direction
    created_at: datetime
    status: OrderStatus = OrderStatus.PENDING
    limit_price: Decimal | None = None
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"order quantity must be positive, got {self.quantity}")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
