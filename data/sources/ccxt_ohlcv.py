"""Live/recent OHLCV via ccxt, for Kraken (spec Section 3.3).

Kraken's OHLC REST endpoint returns at most the 720 most recent
candles no matter what ``since`` you pass — there is no pagination path
to deep history, which is exactly why Binance's bulk dumps (not this
module) supply the 2019+ backfill for crypto. This module exists for
the live/sim feed in later phases and, in Phase 0, to prove the path
works end to end and to store a forward-looking cross-check series
under ``venue=kraken``.

Two non-obvious things, both load-bearing:

- **Kraken's last candle is always the current, still-forming one and
  must be dropped** (confirmed against Kraken's own REST docs); keeping
  it would violate design rule 3 (never act on an incomplete candle).
- **Symbols are spelled with a dash here, not a slash** (``BTC-USD``,
  matching the Parquet store's filesystem-safe convention — see
  ``data/store.py``). This module converts to ccxt's unified
  ``BTC/USD`` spelling only at the API call boundary.
"""

from __future__ import annotations

import ccxt
import pandas as pd

from core.enums import Timeframe, Venue
from data import BAR_COLUMNS

MAX_CANDLES_PER_CALL = 720  # Kraken's hard cap, regardless of `since`.


def _to_ccxt_symbol(symbol: str) -> str:
    return symbol.replace("-", "/")


def make_kraken_exchange() -> ccxt.kraken:
    """Public (unauthenticated) Kraken client. OHLCV is public data —
    no API key needed, and Phase 0 never touches order endpoints."""
    return ccxt.kraken({"enableRateLimit": True})


def fetch_recent_ohlcv(
    symbol: str,
    timeframe: Timeframe,
    exchange: ccxt.Exchange | None = None,
) -> pd.DataFrame:
    """Fetch the most recent closed candles for ``symbol`` from Kraken.

    ``symbol`` uses the project's canonical dash spelling (``BTC-USD``).
    Returns the unified bar schema, sorted ascending, with the
    still-forming final candle already dropped.
    """
    exchange = exchange or make_kraken_exchange()
    if not exchange.has.get("fetchOHLCV"):
        raise RuntimeError(f"{exchange.id} does not support fetchOHLCV")

    ccxt_symbol = _to_ccxt_symbol(symbol)
    raw = exchange.fetch_ohlcv(ccxt_symbol, timeframe=timeframe.value, limit=MAX_CANDLES_PER_CALL)
    if not raw:
        return pd.DataFrame(columns=list(BAR_COLUMNS))

    # Kraken: "the last entry ... is for the current, not-yet-committed
    # timeframe, and will always be present." Drop it.
    closed_candles = raw[:-1]
    if not closed_candles:
        return pd.DataFrame(columns=list(BAR_COLUMNS))

    frame = pd.DataFrame(
        closed_candles, columns=["open_ms", "open", "high", "low", "close", "volume"]
    )
    open_time = pd.to_datetime(frame["open_ms"], unit="ms", utc=True)
    duration = pd.Timedelta(seconds=timeframe.timedelta_seconds)

    return pd.DataFrame(
        {
            "venue": Venue.KRAKEN.value,
            "symbol": symbol,
            "timeframe": timeframe.value,
            "open_time": open_time,
            "close_time": open_time + duration,
            "open": frame["open"].astype(float),
            "high": frame["high"].astype(float),
            "low": frame["low"].astype(float),
            "close": frame["close"].astype(float),
            "volume": frame["volume"].astype(float),
        }
    )
