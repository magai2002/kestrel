"""Spec Section 11, "Adapter contract": every ``LiveBroker`` passes the
same suite. Sandbox and live adapters run against venue fakes that
return the venues' documented structures (``tests/contract/venue_fakes.py``);
real-network versions of the same checks are the opt-in
``-m integration`` tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

import ccxt
import pytest
from ib_async import Contract
from venue_fakes import FakeCcxtExchange, FakeIB

from core.order import OrderSide
from execution.brokers.base import BrokerError, BrokerOrderStatus, LiveBroker, TransientBrokerError
from execution.brokers.ccxt_spot import CcxtSpotBroker
from execution.brokers.client_ids import venue_client_id
from execution.brokers.fake import FakeVenue
from execution.brokers.ibkr import IbkrBroker


@dataclass
class Case:
    broker: LiveBroker
    symbol: str
    price: Decimal
    qty: Decimal
    make_rest: Callable[[], None]  # make the next order rest instead of filling
    inject_transient: Callable[[], None] | None


def _fake_venue() -> Case:
    v = FakeVenue({"BTCUSDT": Decimal("50000")}, balances={"USDT": Decimal(10000)})

    def rest() -> None:
        v.fill_on_place = False

    return Case(v, "BTCUSDT", Decimal("50000"), Decimal("0.01"), rest, None)


def _ccxt(venue_id: str, symbol: str, unified: str) -> Case:
    ex = FakeCcxtExchange(venue_id, {unified: 50000.0})
    broker = CcxtSpotBroker(venue_id, ex, {symbol: unified})

    def rest() -> None:
        ex.fill_immediately = False

    def transient() -> None:
        ex.raise_next = ccxt.RequestTimeout("timed out")

    return Case(broker, symbol, Decimal("50000"), Decimal("0.01"), rest, transient)


def _ibkr() -> Case:
    contract = Contract(
        secType="STK",
        conId=111,
        symbol="VUSD",
        exchange="SMART",
        primaryExchange="LSEETF",
        currency="USD",
    )
    ib = FakeIB({"VUSD": contract}, {"VUSD": 138.5})
    broker = IbkrBroker(ib, {"VUSD": contract}, account="DU123")

    def rest() -> None:
        ib.fill_immediately = False

    def transient() -> None:
        ib.connected = False

    return Case(broker, "VUSD", Decimal("138.5"), Decimal("3"), rest, transient)


CASES = {
    "fake": _fake_venue,
    "binance_testnet": lambda: _ccxt("binance", "BTCUSDT", "BTC/USDT"),
    "kraken": lambda: _ccxt("kraken", "BTC-USD", "BTC/USD"),
    "ibkr": _ibkr,
}


@pytest.fixture(params=list(CASES))
def case(request: pytest.FixtureRequest) -> Case:
    return CASES[request.param]()


def _cid(n: int = 0) -> str:
    return f"crypto-portfolio-20260101T120000Z-X-{n}"


def test_marketable_buy_fills_and_is_visible_by_client_id(case: Case) -> None:
    placed = case.broker.place_limit_order(
        _cid(), case.symbol, OrderSide.BUY, case.qty, case.price * Decimal("1.002")
    )
    assert placed.client_order_id == _cid()
    found = case.broker.get_order(_cid(), case.symbol)
    assert found is not None
    assert found.client_order_id == _cid()
    assert found.status is BrokerOrderStatus.FILLED
    assert found.filled_quantity == case.qty
    assert sum(f.quantity for f in found.fills) == case.qty
    assert all(f.fill_id for f in found.fills)
    assert all(f.price > 0 for f in found.fills)


def test_unknown_client_id_is_none(case: Case) -> None:
    assert case.broker.get_order(_cid(99), case.symbol) is None


def test_resting_order_is_listed_open_then_cancelled(case: Case) -> None:
    case.make_rest()
    case.broker.place_limit_order(
        _cid(1), case.symbol, OrderSide.BUY, case.qty, case.price * Decimal("0.9")
    )
    open_ids = [o.client_order_id for o in case.broker.open_orders([case.symbol])]
    assert _cid(1) in open_ids
    cancelled = case.broker.cancel_order(_cid(1), case.symbol)
    assert cancelled is not None and cancelled.status is BrokerOrderStatus.CANCELLED
    assert cancelled.filled_quantity == 0
    # Cancelling a final order is a no-op, not an error.
    again = case.broker.cancel_order(_cid(1), case.symbol)
    assert again is not None and again.status is BrokerOrderStatus.CANCELLED


def test_positions_cover_every_requested_symbol(case: Case) -> None:
    before = case.broker.positions([case.symbol])[case.symbol]
    case.broker.place_limit_order(
        _cid(2), case.symbol, OrderSide.BUY, case.qty, case.price * Decimal("1.002")
    )
    after = case.broker.positions([case.symbol])[case.symbol]
    assert after - before == case.qty


def test_server_time_is_tz_aware(case: Case) -> None:
    assert case.broker.server_time().tzinfo is not None


def test_last_price_is_positive(case: Case) -> None:
    px = case.broker.last_price(case.symbol)
    assert px is not None and px > 0


def test_network_failures_surface_as_transient(case: Case) -> None:
    if case.inject_transient is None:
        pytest.skip("in-memory venue has no network")
    case.inject_transient()
    with pytest.raises(TransientBrokerError):
        case.broker.server_time()


def test_ccxt_sends_a_venue_safe_uuid_client_id() -> None:
    ex = FakeCcxtExchange("binance", {"BTC/USDT": 50000.0})
    broker = CcxtSpotBroker("binance_testnet", ex, {"BTCUSDT": "BTC/USDT"})
    long_id = "crypto-trend_vol_target-20261120T120000Z-BTCUSDT-0"
    broker.place_limit_order(long_id, "BTCUSDT", OrderSide.BUY, Decimal("0.01"), Decimal("50100"))
    sent = next(iter(ex.orders.values()))["clientOrderId"]
    assert sent == venue_client_id(long_id)
    assert len(sent) == 36  # Binance's newClientOrderId limit


def test_ccxt_definite_exchange_error_is_broker_error_not_transient() -> None:
    ex = FakeCcxtExchange("binance", {"BTC/USDT": 50000.0})
    broker = CcxtSpotBroker("binance_testnet", ex, {"BTCUSDT": "BTC/USDT"})
    ex.raise_next = ccxt.InsufficientFunds("Account has insufficient balance")
    with pytest.raises(BrokerError) as info:
        broker.place_limit_order(_cid(), "BTCUSDT", OrderSide.BUY, Decimal("1"), Decimal("50000"))
    assert not isinstance(info.value, TransientBrokerError)


def test_ibkr_refuses_fractional_shares() -> None:
    case = _ibkr()
    with pytest.raises(BrokerError, match="whole shares"):
        case.broker.place_limit_order(_cid(), "VUSD", OrderSide.BUY, Decimal("1.5"), Decimal("139"))


def test_ibkr_uses_full_client_id_as_order_ref_and_reports_usd_cash() -> None:
    case = _ibkr()
    case.broker.place_limit_order(_cid(5), "VUSD", OrderSide.BUY, Decimal("2"), Decimal("139"))
    assert case.broker.get_order(_cid(5), "VUSD").fills[0].fee == Decimal("1.7")  # type: ignore[union-attr]
    assert case.broker.cash("USD") < Decimal(10_000)
    assert case.broker.cash("EUR") == Decimal(500000)
