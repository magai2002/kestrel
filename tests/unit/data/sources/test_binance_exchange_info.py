"""Fixture below is the real response shape (filter names, field names)
confirmed by a live call to Binance's public exchangeInfo endpoint
during Phase 2 development -- see the module docstring."""

from __future__ import annotations

from decimal import Decimal

import httpx
import respx

from data.sources import binance_exchange_info as bei

BTCUSDT_RAW = {
    "symbol": "BTCUSDT",
    "filters": [
        {
            "filterType": "PRICE_FILTER",
            "minPrice": "0.01000000",
            "maxPrice": "1000000.00000000",
            "tickSize": "0.01000000",
        },
        {
            "filterType": "LOT_SIZE",
            "minQty": "0.00001000",
            "maxQty": "9000.00000000",
            "stepSize": "0.00001000",
        },
        {
            "filterType": "NOTIONAL",
            "minNotional": "5.00000000",
            "applyMinToMarket": True,
            "maxNotional": "9000000.00000000",
            "applyMaxToMarket": False,
            "avgPriceMins": 5,
        },
    ],
}

ETHUSDT_RAW = {
    "symbol": "ETHUSDT",
    "filters": [
        {
            "filterType": "PRICE_FILTER",
            "minPrice": "0.01000000",
            "maxPrice": "1000000.00000000",
            "tickSize": "0.01000000",
        },
        {
            "filterType": "LOT_SIZE",
            "minQty": "0.00010000",
            "maxQty": "9000.00000000",
            "stepSize": "0.00010000",
        },
        {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
    ],
}


def test_parse_symbol_info_matches_real_binance_values() -> None:
    parsed = bei.parse_symbol_info(BTCUSDT_RAW)
    assert parsed.symbol == "BTCUSDT"
    assert parsed.tick_size == Decimal("0.01")
    assert parsed.step_size == Decimal("0.00001")
    assert parsed.min_notional == Decimal("5.00000000")


def test_parse_symbol_info_falls_back_to_min_notional_filter_name() -> None:
    raw = {
        "symbol": "BTCUSDT",
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "stepSize": "0.00001"},
            {"filterType": "MIN_NOTIONAL", "minNotional": "10.0"},
        ],
    }
    parsed = bei.parse_symbol_info(raw)
    assert parsed.min_notional == Decimal("10.0")


def test_parse_symbol_info_raises_if_filter_missing() -> None:
    raw = {"symbol": "BTCUSDT", "filters": []}
    try:
        bei.parse_symbol_info(raw)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


@respx.mock
def test_fetch_filters_for_multiple_symbols() -> None:
    respx.get(f"{bei.BASE_URL}/api/v3/exchangeInfo").mock(
        return_value=httpx.Response(200, json={"symbols": [BTCUSDT_RAW, ETHUSDT_RAW]})
    )
    with httpx.Client() as client:
        result = bei.fetch_filters(["BTCUSDT", "ETHUSDT"], client=client)
    assert set(result) == {"BTCUSDT", "ETHUSDT"}
    assert result["ETHUSDT"].step_size == Decimal("0.0001")
