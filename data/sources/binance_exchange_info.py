"""Binance's public ``exchangeInfo`` endpoint: tick size, step size and
minimum notional per symbol.

Design rule 4 requires rounding orders to "each instrument's tick size,
step size and minimum notional from exchange metadata" — Phase 0 left
these nullable on `core.orm.InstrumentRecord` because nothing fetched
them yet. This module does, verified against a real response rather
than guessed (confirmed by a live call during Phase 2 development):

    BTCUSDT: tickSize=0.01, stepSize=0.00001, minNotional=5.00
    ETHUSDT: tickSize=0.01, stepSize=0.0001,  minNotional=5.00

No authentication needed — this is public reference data, not an
account-scoped endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

BASE_URL = "https://api.binance.com"


@dataclass(frozen=True)
class ExchangeFilters:
    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_notional: Decimal


def _filter_value(filters: list[dict[str, Any]], filter_type: str, key: str) -> str:
    for f in filters:
        if f.get("filterType") == filter_type:
            return str(f[key])
    raise ValueError(f"{filter_type} not found in filters for this symbol")


def parse_symbol_info(raw: dict[str, Any]) -> ExchangeFilters:
    filters = raw["filters"]
    tick_size = Decimal(_filter_value(filters, "PRICE_FILTER", "tickSize"))
    step_size = Decimal(_filter_value(filters, "LOT_SIZE", "stepSize"))
    # Binance has used both "NOTIONAL" and the older "MIN_NOTIONAL" filter
    # names across API versions; accept either.
    try:
        min_notional = Decimal(_filter_value(filters, "NOTIONAL", "minNotional"))
    except ValueError:
        min_notional = Decimal(_filter_value(filters, "MIN_NOTIONAL", "minNotional"))
    return ExchangeFilters(
        symbol=raw["symbol"], tick_size=tick_size, step_size=step_size, min_notional=min_notional
    )


def fetch_filters(
    symbols: list[str], client: httpx.Client | None = None
) -> dict[str, ExchangeFilters]:
    """Fetch tick/step/min-notional for each symbol in one call."""
    owns_client = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        symbols_param = "[" + ",".join(f'"{s}"' for s in symbols) + "]"
        response = client.get(f"{BASE_URL}/api/v3/exchangeInfo", params={"symbols": symbols_param})
        response.raise_for_status()
        data = response.json()
    finally:
        if owns_client:
            client.close()
    return {s["symbol"]: parse_symbol_info(s) for s in data["symbols"]}
