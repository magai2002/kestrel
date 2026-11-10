from datetime import UTC, datetime
from decimal import Decimal

from core.bar import Bar
from core.enums import Timeframe, Venue
from core.order import Order, OrderSide
from execution.brokers.sim import SimBroker
from execution.costs import CostModel


def _bar(open_: float = 50000.0, volume: float = 1000.0) -> Bar:
    return Bar(
        venue=Venue.BINANCE,
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        open_time=datetime(2024, 1, 1, tzinfo=UTC),
        close_time=datetime(2024, 1, 1, 1, tzinfo=UTC),
        open=open_,
        high=open_ * 1.01,
        low=open_ * 0.99,
        close=open_ * 1.005,
        volume=volume,
    )


def _order(quantity: str = "1.0") -> Order:
    return Order(
        client_order_id="x",
        instrument_id="binance:BTCUSDT",
        side=OrderSide.BUY,
        quantity=Decimal(quantity),
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )


def test_fills_using_bar_open_not_close() -> None:
    broker = SimBroker(cost_model=CostModel(spread_bps=Decimal("0"), slippage_k=Decimal("0")))
    fill = broker.submit_order(_order(), _bar(open_=50000.0), trailing_volatility=Decimal("0"))
    assert fill is not None
    assert fill.price == Decimal("50000")  # not bar.close (50250)


def test_fills_at_the_bars_close_time() -> None:
    broker = SimBroker()
    bar = _bar()
    fill = broker.submit_order(_order(), bar, trailing_volatility=Decimal("0"))
    assert fill is not None
    assert fill.timestamp == bar.close_time


def test_trailing_volatility_affects_price_not_the_bars_own_range() -> None:
    """The fill price must depend on the passed-in trailing volatility,
    not on this bar's own high-low range -- using the bar's own range
    to price a fill at its open would be using information from later
    in the same bar (see Broker.submit_order's docstring)."""
    broker = SimBroker(cost_model=CostModel(spread_bps=Decimal("0"), slippage_k=Decimal("1.0")))
    bar = _bar(open_=50000.0)  # has its own high-low range, deliberately ignored
    low_vol_fill = broker.submit_order(_order(), bar, trailing_volatility=Decimal("0.001"))
    high_vol_fill = broker.submit_order(_order(), bar, trailing_volatility=Decimal("0.05"))
    assert low_vol_fill is not None and high_vol_fill is not None
    assert high_vol_fill.price > low_vol_fill.price


def test_zero_volume_bar_fills_nothing() -> None:
    broker = SimBroker()
    fill = broker.submit_order(_order(), _bar(volume=0.0), trailing_volatility=Decimal("0"))
    assert fill is None
