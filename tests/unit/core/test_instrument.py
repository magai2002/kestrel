import pytest
from pydantic import ValidationError

from core.enums import AssetClass, Venue
from core.instrument import Instrument


def test_instrument_id_combines_venue_and_symbol() -> None:
    inst = Instrument(
        venue=Venue.ALPACA,
        symbol="SPY",
        asset_class=AssetClass.EQUITY,
        base_currency="SPY",
        quote_currency="USD",
    )
    assert inst.instrument_id == "alpaca:SPY"
    assert str(inst) == "alpaca:SPY"


def test_rejects_empty_symbol() -> None:
    with pytest.raises(ValidationError):
        Instrument(
            venue=Venue.BINANCE,
            symbol="   ",
            asset_class=AssetClass.CRYPTO,
            base_currency="BTC",
            quote_currency="USDT",
        )


def test_tick_step_min_notional_default_to_none() -> None:
    inst = Instrument(
        venue=Venue.BINANCE,
        symbol="BTCUSDT",
        asset_class=AssetClass.CRYPTO,
        base_currency="BTC",
        quote_currency="USDT",
    )
    assert inst.tick_size is None
    assert inst.step_size is None
    assert inst.min_notional is None
