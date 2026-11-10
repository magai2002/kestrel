"""A held position: quantity and average cost basis for one instrument.

Long-only (design rule 1 — spot and long/flat, no shorts in v1), so
``quantity`` is never negative, and a sell that would take it negative
is a bug upstream (the order manager should never propose one), not
something this type silently clamps.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core.fill import Fill
from core.order import OrderSide


@dataclass(frozen=True)
class Position:
    instrument_id: str
    quantity: Decimal
    avg_price: Decimal  # cost basis of the current quantity; 0 when flat

    def __post_init__(self) -> None:
        if self.quantity < 0:
            raise ValueError(f"quantity must not be negative (long-only), got {self.quantity}")
        if self.avg_price < 0:
            raise ValueError(f"avg_price must not be negative, got {self.avg_price}")

    @classmethod
    def flat(cls, instrument_id: str) -> Position:
        return cls(instrument_id, Decimal(0), Decimal(0))

    def market_value(self, price: Decimal) -> Decimal:
        return self.quantity * price

    def apply_fill(self, fill: Fill) -> Position:
        """Returns a *new* Position (immutable) reflecting the fill."""
        if fill.instrument_id != self.instrument_id:
            raise ValueError(f"fill is for {fill.instrument_id}, not {self.instrument_id}")

        if fill.side == OrderSide.BUY:
            new_quantity = self.quantity + fill.quantity
            new_avg_price = (
                self.quantity * self.avg_price + fill.quantity * fill.price
            ) / new_quantity
            return Position(self.instrument_id, new_quantity, new_avg_price)

        new_quantity = self.quantity - fill.quantity
        if new_quantity < 0:
            raise ValueError(
                f"sell of {fill.quantity} exceeds held {self.quantity} for {self.instrument_id}"
            )
        new_avg_price = self.avg_price if new_quantity > 0 else Decimal(0)
        return Position(self.instrument_id, new_quantity, new_avg_price)
