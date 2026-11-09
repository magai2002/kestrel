from pathlib import Path

import pytest
from pydantic import ValidationError

from config.sleeve import load_sleeve_config
from core.enums import AssetClass, Timeframe, Venue


def test_loads_crypto_sleeve() -> None:
    sleeve = load_sleeve_config(Path("config/sleeves/crypto.yaml"))
    assert sleeve.name == "crypto"
    assert sleeve.asset_class == AssetClass.CRYPTO
    assert sleeve.decision_bar == Timeframe.H4
    assert sleeve.paper_capital == 10_000
    symbols = {(e.venue, e.symbol) for e in sleeve.universe}
    assert (Venue.BINANCE, "BTCUSDT") in symbols
    assert (Venue.BINANCE, "ETHUSDT") in symbols
    assert (Venue.KRAKEN, "BTC-USD") in symbols
    assert (Venue.KRAKEN, "ETH-USD") in symbols


def test_loads_equities_sleeve() -> None:
    sleeve = load_sleeve_config(Path("config/sleeves/equities.yaml"))
    assert sleeve.name == "equities"
    assert sleeve.decision_bar == Timeframe.D1
    symbols = {e.symbol for e in sleeve.universe}
    assert symbols == {"SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC", "VNQ"}


def test_rejects_malformed_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: test\nasset_class: crypto\n")  # missing required fields
    with pytest.raises(ValidationError):
        load_sleeve_config(bad)


def test_kraken_symbols_contain_no_slash() -> None:
    """Hard requirement of the Parquet store (data/store.py): '/' can't
    appear in a symbol used as a partition directory name."""
    sleeve = load_sleeve_config(Path("config/sleeves/crypto.yaml"))
    for entry in sleeve.universe:
        assert "/" not in entry.symbol
