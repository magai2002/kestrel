"""Instrument identity.

Phase 0 only needs enough to label and partition market data. Tick
size, step size and minimum notional (needed for order rounding, design
rule 4 in the spec) are nullable here and get populated when the
execution layer arrives in Phase 2 — we don't have exchange-info
fetchers yet, so pretending we know those numbers today would be a
lie the type system doesn't catch.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, model_validator

from core.enums import AssetClass, Venue


class Instrument(BaseModel):
    """A tradeable thing on a specific venue.

    ``instrument_id`` is the stable string identity used everywhere
    else (Parquet partitions, config files, log lines): ``"{venue}:{symbol}"``,
    e.g. ``"binance:BTCUSDT"`` or ``"alpaca:SPY"``.
    """

    model_config = ConfigDict(frozen=True)

    venue: Venue
    symbol: str
    asset_class: AssetClass
    base_currency: str
    quote_currency: str
    tick_size: Decimal | None = None
    step_size: Decimal | None = None
    min_notional: Decimal | None = None

    @model_validator(mode="after")
    def _non_empty_symbol(self) -> Instrument:
        if not self.symbol.strip():
            raise ValueError("symbol must not be empty")
        return self

    @property
    def instrument_id(self) -> str:
        return f"{self.venue.value}:{self.symbol}"

    def __str__(self) -> str:
        return self.instrument_id
