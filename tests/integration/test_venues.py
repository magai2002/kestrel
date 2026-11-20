"""Opt-in real-network checks (``uv run pytest -m integration``).

Public endpoints run anywhere; private ones skip unless credentials (or
a reachable IB Gateway) are configured. Orders placed here are resting
limits far from the market, cancelled straight away: they exercise
place / query / cancel without ever filling.
"""

from __future__ import annotations

import socket
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal

import ccxt
import httpx
import pytest

from config.settings import Settings
from config.sleeve import load_sleeve
from core.enums import Timeframe, Venue
from core.order import OrderSide
from execution.brokers import binance_testnet, ibkr
from execution.brokers.base import BrokerOrderStatus
from live.feeds import CcxtFeed, YahooFeed

pytestmark = pytest.mark.integration
NOW = lambda: datetime.now(UTC)  # noqa: E731


def test_binance_testnet_public_klines_are_closed_only() -> None:
    sleeve = load_sleeve("crypto")
    listing = sleeve.asset("BTC").listing_for("sandbox")
    feed = CcxtFeed(
        binance_testnet.make_exchange(), Venue.BINANCE_TESTNET, {"BTCUSDT": "BTC/USDT"}, limit=5
    )
    now = NOW()
    df = feed.fetch(listing, Timeframe.H4, now)
    assert len(df) >= 3
    assert (df["close_time"] <= now).all()


def test_kraken_public_ohlc_drops_forming_candle() -> None:
    listing = load_sleeve("crypto").asset("BTC").listing_for("sim")
    feed = CcxtFeed(ccxt.kraken(), Venue.KRAKEN, {"BTC-USD": "BTC/USD"}, limit=10)
    df = feed.fetch(listing, Timeframe.H4, NOW())
    assert (df["close_time"] <= NOW()).all()


def test_yahoo_twin_feed() -> None:
    listing = load_sleeve("equities").asset("SPY").listing_for("sim")
    with httpx.Client(timeout=30) as c:
        df = YahooFeed(c).fetch(listing, Timeframe.D1, NOW())
    assert not df.empty and (df["close_time"] <= NOW()).all()


@pytest.fixture
def testnet_broker():  # type: ignore[no-untyped-def]
    s = Settings()
    if not (s.binance_testnet_api_key and s.binance_testnet_api_secret):
        pytest.skip("KESTREL_BINANCE_TESTNET_API_KEY/_SECRET not set")
    return binance_testnet.make_broker(
        {"BTCUSDT": "BTC/USDT"},
        s.binance_testnet_api_key.get_secret_value(),
        s.binance_testnet_api_secret.get_secret_value(),
    )


def test_binance_testnet_place_query_cancel(testnet_broker) -> None:  # type: ignore[no-untyped-def]
    px = testnet_broker.last_price("BTCUSDT")
    assert px is not None
    far = (px * Decimal("0.7")).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    cid = f"it-crypto-test-{NOW():%Y%m%dT%H%M%S%fZ}-BTCUSDT-0"
    placed = testnet_broker.place_limit_order(cid, "BTCUSDT", OrderSide.BUY, Decimal("0.0002"), far)
    assert placed.status is BrokerOrderStatus.OPEN
    found = testnet_broker.get_order(cid, "BTCUSDT")
    assert found is not None and found.client_order_id == cid
    cancelled = testnet_broker.cancel_order(cid, "BTCUSDT")
    assert cancelled is not None and cancelled.status is BrokerOrderStatus.CANCELLED
    assert abs((testnet_broker.server_time() - NOW()).total_seconds()) < 10


def _gateway_up(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def test_ibkr_paper_qualify_place_cancel() -> None:
    s = Settings()
    if not _gateway_up(s.ibkr_host, s.ibkr_paper_port):
        pytest.skip(f"no IB Gateway on {s.ibkr_host}:{s.ibkr_paper_port}")
    sleeve = load_sleeve("equities")
    listing = sleeve.asset("SPY").listing_for("sandbox")
    ib = ibkr.connect(s.ibkr_host, s.ibkr_paper_port, s.ibkr_client_id + 20)
    try:
        contracts = ibkr.qualify_contracts(
            ib, {listing.symbol: (listing.exchange or "SMART", "USD")}
        )
        account = s.ibkr_paper_account.get_secret_value() if s.ibkr_paper_account else ""
        broker = ibkr.IbkrBroker(ib, contracts, account)
        cid = f"it-equities-test-{NOW():%Y%m%dT%H%M%S%fZ}-{listing.symbol}-0"
        broker.place_limit_order(cid, listing.symbol, OrderSide.BUY, Decimal(1), Decimal("1.00"))
        assert broker.get_order(cid, listing.symbol) is not None
        cancelled = broker.cancel_order(cid, listing.symbol)
        assert cancelled is not None and cancelled.status in (
            BrokerOrderStatus.CANCELLED,
            BrokerOrderStatus.OPEN,
        )
    finally:
        ib.disconnect()
