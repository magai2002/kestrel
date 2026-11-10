from datetime import UTC, datetime
from decimal import Decimal

from core.order import Order, OrderSide
from execution.costs import CostModel
from execution.fills import (
    build_market_fill,
    build_passive_limit_fill,
    capped_fill_quantity,
    market_fill_price,
    trades_through_limit,
)


def _order(side: OrderSide = OrderSide.BUY, quantity: str = "1.0") -> Order:
    return Order(
        client_order_id="x",
        instrument_id="binance:BTCUSDT",
        side=side,
        quantity=Decimal(quantity),
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )


def test_market_fill_price_buy_pays_up() -> None:
    cost_model = CostModel(spread_bps=Decimal("2"), slippage_k=Decimal("0"))
    price = market_fill_price(Decimal("50000"), OrderSide.BUY, Decimal("0"), cost_model)
    assert price > Decimal("50000")


def test_market_fill_price_sell_receives_less() -> None:
    cost_model = CostModel(spread_bps=Decimal("2"), slippage_k=Decimal("0"))
    price = market_fill_price(Decimal("50000"), OrderSide.SELL, Decimal("0"), cost_model)
    assert price < Decimal("50000")


def test_market_fill_price_hand_computed() -> None:
    # half-spread = 2bps/2 * 50000 = 5; slippage = 0.1*0.02*50000 = 100
    cost_model = CostModel(spread_bps=Decimal("2"), slippage_k=Decimal("0.1"))
    price = market_fill_price(Decimal("50000"), OrderSide.BUY, Decimal("0.02"), cost_model)
    assert price == Decimal("50105.0")


def test_capped_fill_quantity_under_cap_fills_fully() -> None:
    assert capped_fill_quantity(Decimal("1"), Decimal("1000"), Decimal("0.05")) == Decimal("1")


def test_capped_fill_quantity_caps_at_participation() -> None:
    # 5% of 1000 volume = 50, less than the 100 requested.
    assert capped_fill_quantity(Decimal("100"), Decimal("1000"), Decimal("0.05")) == Decimal("50")


def test_trades_through_limit_buy_requires_strictly_below() -> None:
    assert (
        trades_through_limit(OrderSide.BUY, Decimal("100"), Decimal("105"), Decimal("100")) is False
    )
    assert (
        trades_through_limit(OrderSide.BUY, Decimal("100"), Decimal("105"), Decimal("99")) is True
    )


def test_trades_through_limit_sell_requires_strictly_above() -> None:
    assert (
        trades_through_limit(OrderSide.SELL, Decimal("100"), Decimal("100"), Decimal("95")) is False
    )
    assert (
        trades_through_limit(OrderSide.SELL, Decimal("100"), Decimal("101"), Decimal("95")) is True
    )


def test_build_market_fill_hand_computed() -> None:
    cost_model = CostModel(
        fee_pct=Decimal("0.008"), spread_bps=Decimal("0"), slippage_k=Decimal("0")
    )
    fill = build_market_fill(
        order=_order(quantity="1.0"),
        open_price=Decimal("50000"),
        bar_volume=Decimal("1000"),
        bar_volatility=Decimal("0"),
        cost_model=cost_model,
        participation_cap=Decimal("1.0"),
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert fill is not None
    assert fill.quantity == Decimal("1.0")
    assert fill.price == Decimal("50000")
    assert fill.fee == Decimal("400.000")  # 50000 * 0.008


def test_build_market_fill_none_on_zero_volume() -> None:
    cost_model = CostModel()
    fill = build_market_fill(
        order=_order(),
        open_price=Decimal("50000"),
        bar_volume=Decimal("0"),
        bar_volatility=Decimal("0"),
        cost_model=cost_model,
        participation_cap=Decimal("0.05"),
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert fill is None


def test_build_market_fill_partial_under_participation_cap() -> None:
    cost_model = CostModel(spread_bps=Decimal("0"), slippage_k=Decimal("0"))
    fill = build_market_fill(
        order=_order(quantity="100"),
        open_price=Decimal("50000"),
        bar_volume=Decimal("1000"),
        bar_volatility=Decimal("0"),
        cost_model=cost_model,
        participation_cap=Decimal("0.05"),
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert fill is not None
    assert fill.quantity == Decimal("50")  # 5% of 1000


def test_build_passive_limit_fill_none_when_not_traded_through() -> None:
    fill = build_passive_limit_fill(
        order=_order(side=OrderSide.BUY),
        limit_price=Decimal("100"),
        bar_high=Decimal("105"),
        bar_low=Decimal("101"),
        bar_volume=Decimal("1000"),
        cost_model=CostModel(),
        participation_cap=Decimal("1.0"),
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert fill is None


def test_build_passive_limit_fill_fills_at_limit_price_when_traded_through() -> None:
    fill = build_passive_limit_fill(
        order=_order(side=OrderSide.BUY, quantity="1.0"),
        limit_price=Decimal("100"),
        bar_high=Decimal("105"),
        bar_low=Decimal("95"),
        bar_volume=Decimal("1000"),
        cost_model=CostModel(fee_pct=Decimal("0")),
        participation_cap=Decimal("1.0"),
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert fill is not None
    assert fill.price == Decimal("100")  # fills AT the limit, not at some adjusted price
