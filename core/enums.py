"""Enumerations shared by every layer that talks about instruments or bars."""

from __future__ import annotations

from enum import StrEnum


class Venue(StrEnum):
    """Where a price or an order originates.

    ``SIM`` is not a real venue: it labels bars synthesized by
    resampling (e.g. 4h/1d bars derived from Binance 1h history) so the
    Parquet partition records honestly that the bar did not come
    straight off the wire.
    """

    BINANCE = "binance"
    KRAKEN = "kraken"
    ALPACA = "alpaca"


class AssetClass(StrEnum):
    CRYPTO = "crypto"
    EQUITY = "equity"


class Timeframe(StrEnum):
    """Bar interval. Values are pandas-compatible offset aliases."""

    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def timedelta_seconds(self) -> int:
        return {
            Timeframe.H1: 3600,
            Timeframe.H4: 4 * 3600,
            Timeframe.D1: 24 * 3600,
        }[self]

    @property
    def pandas_freq(self) -> str:
        """Alias accepted by ``pandas.date_range``/``resample``."""
        return {
            Timeframe.H1: "1h",
            Timeframe.H4: "4h",
            Timeframe.D1: "1D",
        }[self]
