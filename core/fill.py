"""A Fill: the broker's response to an order, or part of one (partial
fills are proportional to bar volume — ``execution/fills.py``)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core.order import OrderSide


@dataclass(frozen=True)
class Fill:
    client_order_id: str
    instrument_id: str
    side: OrderSide
    quantity: Decimal  # positive; `side` carries direction
    price: Decimal
    fee: Decimal
    timestamp: datetime

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"fill quantity must be positive, got {self.quantity}")
        if self.price <= 0:
            raise ValueError(f"fill price must be positive, got {self.price}")
        if self.fee < 0:
            raise ValueError(f"fee must not be negative, got {self.fee}")
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price
