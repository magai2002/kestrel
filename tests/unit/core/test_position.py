from datetime import UTC, datetime
from decimal import Decimal

import pytest

from core.fill import Fill
from core.order import OrderSide
from core.position import Position


def _fill(side: OrderSide, quantity: str, price: str) -> Fill:
    return Fill(
        client_order_id="x",
        instrument_id="binance:BTCUSDT",
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fee=Decimal("0"),
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
    )


def test_flat_position() -> None:
    pos = Position.flat("binance:BTCUSDT")
    assert pos.quantity == 0
    assert pos.avg_price == 0


def test_buy_into_flat_sets_avg_price_to_fill_price() -> None:
    pos = Position.flat("binance:BTCUSDT")
    new_pos = pos.apply_fill(_fill(OrderSide.BUY, "1", "50000"))
    assert new_pos.quantity == Decimal("1")
    assert new_pos.avg_price == Decimal("50000")


def test_buy_more_computes_weighted_average_hand_computed() -> None:
    pos = Position("binance:BTCUSDT", Decimal("1"), Decimal("50000"))
    # 1 @ 50000, buy 1 @ 60000 -> avg = (1*50000 + 1*60000) / 2 = 55000
    new_pos = pos.apply_fill(_fill(OrderSide.BUY, "1", "60000"))
    assert new_pos.quantity == Decimal("2")
    assert new_pos.avg_price == Decimal("55000")


def test_partial_sell_keeps_avg_price_unchanged() -> None:
    pos = Position("binance:BTCUSDT", Decimal("2"), Decimal("55000"))
    new_pos = pos.apply_fill(
        _fill(OrderSide.SELL, "1", "70000")
    )  # sell price irrelevant to cost basis
    assert new_pos.quantity == Decimal("1")
    assert new_pos.avg_price == Decimal("55000")


def test_full_sell_resets_avg_price_to_zero() -> None:
    pos = Position("binance:BTCUSDT", Decimal("1"), Decimal("55000"))
    new_pos = pos.apply_fill(_fill(OrderSide.SELL, "1", "70000"))
    assert new_pos.quantity == Decimal("0")
    assert new_pos.avg_price == Decimal("0")


def test_sell_more_than_held_raises() -> None:
    pos = Position("binance:BTCUSDT", Decimal("1"), Decimal("50000"))
    with pytest.raises(ValueError, match="exceeds held"):
        pos.apply_fill(_fill(OrderSide.SELL, "2", "50000"))


def test_fill_for_different_instrument_raises() -> None:
    pos = Position("binance:BTCUSDT", Decimal("1"), Decimal("50000"))
    other_fill = Fill(
        client_order_id="x",
        instrument_id="binance:ETHUSDT",
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        price=Decimal("3000"),
        fee=Decimal("0"),
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="binance:ETHUSDT"):
        pos.apply_fill(other_fill)


def test_rejects_negative_quantity() -> None:
    with pytest.raises(ValueError, match="long-only"):
        Position("binance:BTCUSDT", Decimal("-1"), Decimal("0"))


def test_market_value() -> None:
    pos = Position("binance:BTCUSDT", Decimal("2"), Decimal("50000"))
    assert pos.market_value(Decimal("60000")) == Decimal("120000")
