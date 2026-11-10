from datetime import UTC, datetime
from decimal import Decimal

from core.order import OrderSide
from execution.order_manager import propose_order, propose_orders

NOW = datetime(2024, 1, 1, 4, 0, tzinfo=UTC)


def test_no_order_when_target_equals_held() -> None:
    order = propose_order(
        "binance:BTCUSDT", "BTCUSDT", Decimal("1.0"), Decimal("1.0"), "crypto", "trend", NOW
    )
    assert order is None


def test_buy_order_when_target_above_held() -> None:
    order = propose_order(
        "binance:BTCUSDT", "BTCUSDT", Decimal("1.5"), Decimal("1.0"), "crypto", "trend", NOW
    )
    assert order is not None
    assert order.side == OrderSide.BUY
    assert order.quantity == Decimal("0.5")


def test_sell_order_when_target_below_held() -> None:
    order = propose_order(
        "binance:BTCUSDT", "BTCUSDT", Decimal("0.3"), Decimal("1.0"), "crypto", "trend", NOW
    )
    assert order is not None
    assert order.side == OrderSide.SELL
    assert order.quantity == Decimal("0.7")


def test_client_order_id_is_deterministic_for_same_inputs() -> None:
    order1 = propose_order(
        "binance:BTCUSDT", "BTCUSDT", Decimal("1.5"), Decimal("1.0"), "crypto", "trend", NOW
    )
    order2 = propose_order(
        "binance:BTCUSDT", "BTCUSDT", Decimal("1.5"), Decimal("1.0"), "crypto", "trend", NOW
    )
    assert order1.client_order_id == order2.client_order_id


def test_propose_orders_skips_instruments_already_at_target() -> None:
    orders = propose_orders(
        target_quantities={"binance:BTCUSDT": Decimal("1.0"), "binance:ETHUSDT": Decimal("2.0")},
        held_quantities={"binance:BTCUSDT": Decimal("1.0"), "binance:ETHUSDT": Decimal("1.0")},
        symbols={"binance:BTCUSDT": "BTCUSDT", "binance:ETHUSDT": "ETHUSDT"},
        sleeve="crypto",
        strategy="trend",
        bar_close_ts=NOW,
    )
    assert len(orders) == 1
    assert orders[0].instrument_id == "binance:ETHUSDT"


def test_propose_orders_treats_absent_held_as_zero() -> None:
    orders = propose_orders(
        target_quantities={"binance:BTCUSDT": Decimal("1.0")},
        held_quantities={},
        symbols={"binance:BTCUSDT": "BTCUSDT"},
        sleeve="crypto",
        strategy="trend",
        bar_close_ts=NOW,
    )
    assert len(orders) == 1
    assert orders[0].side == OrderSide.BUY
    assert orders[0].quantity == Decimal("1.0")
