from datetime import UTC, datetime
from decimal import Decimal

import pytest

from core.fill import Fill
from core.order import OrderSide


def _fill(**overrides: object) -> Fill:
    defaults: dict[str, object] = dict(
        client_order_id="crypto-x-20240101T000000Z-BTCUSDT-0",
        instrument_id="binance:BTCUSDT",
        side=OrderSide.BUY,
        quantity=Decimal("0.5"),
        price=Decimal("50000"),
        fee=Decimal("10"),
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
    )
    defaults.update(overrides)
    return Fill(**defaults)  # type: ignore[arg-type]


def test_notional_hand_computed() -> None:
    assert _fill().notional == Decimal("25000")


def test_rejects_non_positive_quantity() -> None:
    with pytest.raises(ValueError, match="quantity"):
        _fill(quantity=Decimal("0"))


def test_rejects_non_positive_price() -> None:
    with pytest.raises(ValueError, match="price"):
        _fill(price=Decimal("-1"))


def test_rejects_negative_fee() -> None:
    with pytest.raises(ValueError, match="fee"):
        _fill(fee=Decimal("-0.01"))


def test_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="aware"):
        _fill(timestamp=datetime(2024, 1, 1))
