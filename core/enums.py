"""Enumerations shared by every layer that talks about instruments or bars."""

from __future__ import annotations

from enum import StrEnum


class Venue(StrEnum):
    """Where a price or an order originates.

    ``BINANCE_TESTNET`` is its own venue, not a mode flag on ``BINANCE``:
    testnet prices are synthetic and drift from the real market, so its
    bars must never land in the same Parquet partition as real Binance
    history. ``YAHOO`` is a data-only source (no orders); ``IBKR`` is
    Interactive Brokers, the equities broker for paper and live (see
    ``docs/adr/0004-ibkr-ucits-hybrid-data.md``).
    """

    BINANCE = "binance"
    BINANCE_TESTNET = "binance_testnet"
    KRAKEN = "kraken"
    YAHOO = "yahoo"
    IBKR = "ibkr"


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
