"""Tick size, step size and minimum notional from ccxt's public
``load_markets()`` — for Kraken (sim/live) and the Binance Spot Testnet
(sandbox). Real Binance uses ``binance_exchange_info.py`` (its raw
filters), unchanged since Phase 2.

Verified (ccxt 4.5.92): both exchanges use ``precisionMode ==
TICK_SIZE``, so ``precision.price`` *is* the tick and
``precision.amount`` *is* the step (not a count of decimal places).
Kraken BTC/USD: tick 0.1, step 1e-8, ``limits.cost.min`` 0.5 USD,
``limits.amount.min`` 6e-5 BTC — the amount minimum isn't modelled
separately (at a $10,000 sleeve with a 10% buffer every order is
hundreds of dollars), documented in ``docs/api-notes.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import ccxt


@dataclass(frozen=True)
class MarketFilters:
    tick_size: Decimal
    step_size: Decimal
    min_notional: Decimal


def fetch_filters(exchange: Any, symbols: dict[str, str]) -> dict[str, MarketFilters]:
    """``symbols``: our symbol -> ccxt unified symbol."""
    if exchange.precisionMode != ccxt.TICK_SIZE:
        raise RuntimeError(
            f"{exchange.id} precisionMode is {exchange.precisionMode}, not TICK_SIZE; "
            "precision values would be decimal counts, not tick sizes"
        )
    markets = exchange.load_markets()
    out = {}
    for ours, unified in symbols.items():
        m = markets[unified]
        out[ours] = MarketFilters(
            tick_size=Decimal(str(m["precision"]["price"])),
            step_size=Decimal(str(m["precision"]["amount"])),
            min_notional=Decimal(str((m["limits"].get("cost") or {}).get("min") or 0)),
        )
    return out
