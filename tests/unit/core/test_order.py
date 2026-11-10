from datetime import UTC, datetime
from decimal import Decimal

import pytest

from core.order import Order, OrderSide, OrderStatus, make_client_order_id


def test_make_client_order_id_format() -> None:
    client_id = make_client_order_id(
        sleeve="crypto",
        strategy="trend_vol_target",
        bar_close_ts=datetime(2024, 3, 15, 4, 0, tzinfo=UTC),
        symbol="BTCUSDT",
        seq=0,
    )
    assert client_id == "crypto-trend_vol_target-20240315T040000Z-BTCUSDT-0"


def test_make_client_order_id_is_deterministic() -> None:
    kwargs = dict(
        sleeve="crypto",
        strategy="trend_vol_target",
        bar_close_ts=datetime(2024, 3, 15, 4, 0, tzinfo=UTC),
        symbol="BTCUSDT",
        seq=1,
    )
    assert make_client_order_id(**kwargs) == make_client_order_id(**kwargs)


def test_make_client_order_id_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="aware"):
        make_client_order_id("crypto", "x", datetime(2024, 1, 1), "BTCUSDT", 0)


def _order(**overrides: object) -> Order:
    defaults: dict[str, object] = dict(
        client_order_id="crypto-x-20240101T000000Z-BTCUSDT-0",
        instrument_id="binance:BTCUSDT",
        side=OrderSide.BUY,
        quantity=Decimal("0.5"),
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    defaults.update(overrides)
    return Order(**defaults)  # type: ignore[arg-type]


def test_order_defaults_to_pending() -> None:
    assert _order().status == OrderStatus.PENDING


def test_order_rejects_non_positive_quantity() -> None:
    with pytest.raises(ValueError, match="positive"):
        _order(quantity=Decimal("0"))


def test_order_rejects_naive_created_at() -> None:
    with pytest.raises(ValueError, match="aware"):
        _order(created_at=datetime(2024, 1, 1))


def test_order_is_immutable_but_replaceable() -> None:
    import dataclasses

    order = _order()
    submitted = dataclasses.replace(order, status=OrderStatus.SUBMITTED)
    assert order.status == OrderStatus.PENDING  # original untouched
    assert submitted.status == OrderStatus.SUBMITTED
    assert submitted.client_order_id == order.client_order_id
