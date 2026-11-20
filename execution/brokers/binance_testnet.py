"""Binance Spot Testnet (``sandbox`` mode, crypto). Plumbing only:
testnet prices are synthetic (real-data example in ``docs/api-notes.md``:
a 17% wick on one 4h BTC bar), the book is thin, and the testnet is
wiped periodically — which the reconciler will (correctly) report as a
mismatch; see the runbook's "testnet was reset" entry.

Verified in ccxt 4.5.x source: ``set_sandbox_mode(True)`` swaps the spot
REST base to ``https://testnet.binance.vision/api/v3``.
"""

from __future__ import annotations

import ccxt

from execution.brokers.ccxt_spot import CcxtSpotBroker


def make_exchange(api_key: str | None = None, api_secret: str | None = None) -> ccxt.binance:
    ex = ccxt.binance(
        {
            "apiKey": api_key or "",
            "secret": api_secret or "",
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
    )
    ex.set_sandbox_mode(True)
    return ex


def make_broker(symbols: dict[str, str], api_key: str, api_secret: str) -> CcxtSpotBroker:
    ex = make_exchange(api_key, api_secret)
    ex.load_markets()
    return CcxtSpotBroker("binance_testnet", ex, symbols)
