from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from config.sleeve import load_sleeve, load_sleeve_config
from core.enums import AssetClass, Timeframe, Venue

US_ETFS = {"SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC", "VNQ"}


def test_loads_crypto_sleeve() -> None:
    sleeve = load_sleeve("crypto")
    assert sleeve.name == "crypto"
    assert sleeve.asset_class == AssetClass.CRYPTO
    assert sleeve.decision_bar == Timeframe.H4
    assert sleeve.paper_capital == 10_000
    assert sleeve.bars_per_day == 6
    assert sleeve.periods_per_year == 365 * 6
    btc = sleeve.asset("BTC")
    assert (btc.research.venue, btc.research.symbol) == (Venue.BINANCE, "BTCUSDT")
    assert (btc.live.venue, btc.live.symbol) == (Venue.KRAKEN, "BTC-USD")
    assert btc.listing_for("sandbox").venue == Venue.BINANCE_TESTNET


def test_loads_equities_sleeve_research_us_trade_ucits() -> None:
    sleeve = load_sleeve("equities")
    assert sleeve.decision_bar == Timeframe.D1
    assert set(sleeve.asset_ids) == US_ETFS
    for asset in sleeve.assets:
        assert asset.research.venue == Venue.YAHOO
        assert asset.research.symbol == asset.id
        assert asset.live.venue == Venue.IBKR
        assert asset.live.quote_currency == "USD"
        assert asset.live.calendar == "LSE"
        assert asset.live.backfill_symbol and asset.live.backfill_symbol.endswith(".L")
        # IBKR paper (sandbox) trades the same contract as live.
        assert asset.listing_for("sandbox") == asset.live
    assert sleeve.asset("SPY").live.symbol == "VUSD"


def test_equities_costs_have_ibkr_minimum_and_tob() -> None:
    sleeve = load_sleeve("equities")
    for asset in sleeve.assets:
        cm = asset.costs.to_cost_model()
        assert cm.min_fee > 0
        assert cm.tob_pct in {cm.tob_pct.__class__("0.0012"), cm.tob_pct.__class__("0.0132")}


def test_strategy_weights_sum_to_one() -> None:
    for name in ("crypto", "equities"):
        sleeve = load_sleeve(name)
        assert abs(sum(s.weight for s in sleeve.strategies) - 1.0) < 1e-9


def test_rejects_malformed_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: test\nasset_class: crypto\n")  # missing required fields
    with pytest.raises(ValidationError):
        load_sleeve_config(bad)


def test_rejects_group_cap_naming_unknown_asset(tmp_path: Path) -> None:
    raw = yaml.safe_load(Path("config/sleeves/crypto.yaml").read_text())
    raw["risk"]["group_caps"] = [{"name": "g", "assets": ["DOGE"], "cap": 0.5}]
    path = tmp_path / "x.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValidationError, match="unknown assets"):
        load_sleeve_config(path)


def test_rejects_strategy_weights_not_summing_to_one(tmp_path: Path) -> None:
    raw = yaml.safe_load(Path("config/sleeves/crypto.yaml").read_text())
    raw["strategies"] = [{"name": "trend_vol_target", "weight": 0.5}]
    path = tmp_path / "x.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValidationError, match="sum to 1"):
        load_sleeve_config(path)


def test_symbols_contain_no_slash() -> None:
    """Hard requirement of the Parquet store (data/store.py): '/' can't
    appear in a symbol used as a partition directory name."""
    for name in ("crypto", "equities"):
        for asset in load_sleeve(name).assets:
            for listing in (asset.research, asset.live, asset.sandbox):
                if listing is not None:
                    assert "/" not in listing.symbol
