"""The OHLCV bar: the one event every strategy, in every mode, reacts to.

Prices and volume are ``float`` here, not ``Decimal``. Design rule 4 in
the spec reserves ``Decimal`` for orders, fills, the ledger and anything
sent to a broker; a bar is market data, not money changing hands, and it
feeds signal maths (design rule 4 explicitly allows floats there) and a
Parquet/DuckDB storage path where ``float64`` is the native, efficient
column type. Decimal rounding to tick/step size happens later, in the
portfolio and order-manager layers, once a target quantity is about to
become an order.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from core.enums import Timeframe, Venue


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware (got a naive datetime)")
    if value.utcoffset() != UTC.utcoffset(None):
        raise ValueError(f"{field_name} must be in UTC (got offset {value.utcoffset()})")
    return value


class Bar(BaseModel):
    """One completed OHLCV bar.

    ``close_time`` is when the bar finished forming; design rule 3 says
    a strategy may only see bars whose ``close_time <= now``. This type
    does not enforce that itself (it has no notion of "now") — the live
    runner and the no-look-ahead property tests do.
    """

    model_config = ConfigDict(frozen=True)

    venue: Venue
    symbol: str
    timeframe: Timeframe
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @field_validator("open_time", "close_time")
    @classmethod
    def _tz_aware_utc(cls, value: datetime, info: object) -> datetime:
        field_name = getattr(info, "field_name", "timestamp")
        return _require_utc(value, field_name)

    @model_validator(mode="after")
    def _consistent_ohlc(self) -> Bar:
        if self.close_time <= self.open_time:
            raise ValueError(
                f"close_time ({self.close_time}) must be after open_time ({self.open_time})"
            )
        if self.high < max(self.open, self.close, self.low):
            raise ValueError(f"high ({self.high}) is below open/close/low")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError(f"low ({self.low}) is above open/close/high")
        if self.volume < 0:
            raise ValueError(f"volume ({self.volume}) must not be negative")
        return self

    @property
    def instrument_id(self) -> str:
        return f"{self.venue.value}:{self.symbol}"
