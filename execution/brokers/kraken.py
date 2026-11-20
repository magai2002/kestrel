"""Kraken spot (``live`` mode, crypto). **Phase 7-gated**: nothing in the
codebase constructs this outside ``live/gates.py``'s checked path, and
``live`` mode refuses to start unless every spec Section 13 gate passes.

Contract-tested against ccxt's documented unified order/trade
structures only (``tests/contract/``) — no live Kraken keys exist
before Phase 7 (spec rule 4).
"""

from __future__ import annotations

import ccxt

from execution.brokers.ccxt_spot import CcxtSpotBroker


def make_broker(symbols: dict[str, str], api_key: str, api_secret: str) -> CcxtSpotBroker:
    ex = ccxt.kraken({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True})
    ex.load_markets()
    return CcxtSpotBroker("kraken", ex, symbols)
