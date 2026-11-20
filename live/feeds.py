"""Live data feeds: fetch the newest **closed** bars for a listing,
persist them to the Parquet store under their true venue (the nightly
shadow backtest replays exactly these bars — spec Phase 4/6), and hand
them back.

Closed-bar confirmation (design rule 3) is each feed's job, because each
venue signals "still forming" differently — all three verified on real
responses (``docs/api-notes.md``):

- **Kraken OHLC**: the last candle is always the current one; drop it
  (``data/sources/ccxt_ohlcv.py``).
- **Binance (testnet included) klines**: the last candle is the current
  one *if its close time is in the future* — dropped by comparing
  ``open_time + interval`` with ``now``.
- **Yahoo**: returns today's session mid-session; dropped when its
  session close (from the exchange calendar) is after ``now``.
- **IBKR** historical daily bars: dropped by the same calendar rule.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import httpx
import pandas as pd

from config.sleeve import Listing
from core.enums import Timeframe, Venue
from data import BAR_COLUMNS, store
from data.calendars import session_schedule
from data.sources import yahoo_chart


class Feed(Protocol):
    name: str

    def fetch(self, listing: Listing, timeframe: Timeframe, now: datetime) -> pd.DataFrame:
        """Closed bars only, unified schema, ascending."""
        ...


def closed_only(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    if df.empty:
        return df
    return df[df["close_time"] <= pd.Timestamp(now)].reset_index(drop=True)


class CcxtFeed:
    """Kraken (sim) or the Binance testnet (sandbox), via ccxt public OHLCV."""

    def __init__(
        self, exchange: Any, venue: Venue, ccxt_symbols: dict[str, str], limit: int = 500
    ) -> None:
        self._ex = exchange
        self.venue = venue
        self.name = f"ccxt:{venue.value}"
        self._symbols = ccxt_symbols
        self._limit = limit

    def fetch(self, listing: Listing, timeframe: Timeframe, now: datetime) -> pd.DataFrame:
        raw = self._ex.fetch_ohlcv(
            self._symbols[listing.symbol], timeframe=timeframe.value, limit=self._limit
        )
        if not raw:
            return pd.DataFrame(columns=list(BAR_COLUMNS))
        frame = pd.DataFrame(raw, columns=["open_ms", "open", "high", "low", "close", "volume"])
        open_time = pd.to_datetime(frame["open_ms"], unit="ms", utc=True)
        df = pd.DataFrame(
            {
                "venue": self.venue.value,
                "symbol": listing.symbol,
                "timeframe": timeframe.value,
                "open_time": open_time,
                "close_time": open_time + pd.Timedelta(seconds=timeframe.timedelta_seconds),
                **{c: frame[c].astype(float) for c in ("open", "high", "low", "close", "volume")},
            }
        )
        if self.venue == Venue.KRAKEN:
            df = df.iloc[:-1]  # Kraken: last candle is always the forming one
        return closed_only(df, now)


class YahooFeed:
    """UCITS twins (VUSD.L ...) from Yahoo — the sim feed until an IB
    Gateway with an LSE market-data subscription is available."""

    name = "yahoo"

    def __init__(self, client: httpx.Client, lookback_days: int = 20) -> None:
        self._client = client
        self._lookback = lookback_days

    def fetch(self, listing: Listing, timeframe: Timeframe, now: datetime) -> pd.DataFrame:
        symbol = (
            listing.backfill_symbol if listing.backfill_venue == Venue.YAHOO else listing.symbol
        )
        assert symbol is not None
        start = (now - timedelta(days=self._lookback)).date()
        return yahoo_chart.fetch_daily_bars(
            symbol, start, now.date(), listing.calendar or "NYSE", self._client, now
        )


class IbkrFeed:
    """Daily bars from IB Gateway (``reqHistoricalData``, TRADES, RTH)."""

    name = "ibkr"

    def __init__(self, ib: Any, contracts: dict[str, Any], duration: str = "1 M") -> None:
        self._ib = ib
        self._contracts = contracts
        self._duration = duration

    def fetch(self, listing: Listing, timeframe: Timeframe, now: datetime) -> pd.DataFrame:
        if timeframe != Timeframe.D1:
            raise ValueError("IbkrFeed only serves daily bars")
        bars = self._ib.reqHistoricalData(
            self._contracts[listing.symbol],
            endDateTime="",
            durationStr=self._duration,
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=2,
        )
        if not bars:
            return pd.DataFrame(columns=list(BAR_COLUMNS))
        days = [
            b.date
            if isinstance(b.date, date) and not isinstance(b.date, datetime)
            else b.date.date()
            for b in bars
        ]
        sched = session_schedule(
            listing.calendar or "LSE", pd.Timestamp(min(days)), pd.Timestamp(max(days))
        )
        sessions = {
            ts.date(): (o, c)
            for ts, o, c in zip(
                pd.DatetimeIndex(sched.index),
                sched["market_open"],
                sched["market_close"],
                strict=True,
            )
        }
        rows = []
        for b, d in zip(bars, days, strict=True):
            if d not in sessions:
                continue
            rows.append(
                {
                    "venue": Venue.IBKR.value,
                    "symbol": listing.symbol,
                    "timeframe": "1d",
                    "open_time": sessions[d][0],
                    "close_time": sessions[d][1],
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": float(b.volume),
                }
            )
        return closed_only(pd.DataFrame(rows, columns=list(BAR_COLUMNS)), now)


def fetch_and_persist(
    feed: Feed, listing: Listing, timeframe: Timeframe, now: datetime, data_dir: Path
) -> pd.DataFrame:
    df = feed.fetch(listing, timeframe, now)
    if not df.empty:
        store.upsert_bars(df, data_dir)
    return df
