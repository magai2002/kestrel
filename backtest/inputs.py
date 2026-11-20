"""Everything a backtest, a validation run or the live shadow backtest
needs, built from one sleeve config — so all three agree on prices,
instruments, costs and strategy params by construction rather than by
three copies of the same setup code drifting apart.

Prices are keyed by **asset id** (``BTC``, ``SPY``), not venue-specific
instrument id, so strategy params (``mean_reversion``'s ``symbol``,
risk group caps) mean the same thing in every mode.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config.sleeve import Listing, SleeveConfig
from core.instrument import Instrument
from core.orm import InstrumentRecord
from data import store
from execution.costs import CostModel
from strategies.base import Strategy
from strategies.registry import get as get_strategy_entry


class MissingDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class PriceSet:
    """Per-asset OHLCV frames on one shared bar grid (the engine's requirement)."""

    bars: dict[str, pd.DataFrame]
    dropped_bars: dict[str, int]  # bars removed per asset to align the grid

    @property
    def close_panel(self) -> pd.DataFrame:
        """Close prices, index = bar close time, columns = asset ids."""
        first = next(iter(self.bars.values()))
        return pd.DataFrame(
            {aid: df["close"].to_numpy() for aid, df in self.bars.items()},
            index=pd.DatetimeIndex(first["close_time"]),
        )


def align(frames: dict[str, pd.DataFrame]) -> PriceSet:
    """Intersect ``open_time`` across assets so every asset shares a grid.

    Equities on one exchange calendar almost always already agree; a
    single missing print on one ETF would otherwise make the engine
    refuse the whole run. What was dropped is reported, not hidden.
    """
    common: pd.Index | None = None
    for df in frames.values():
        idx = pd.Index(df["open_time"])
        common = idx if common is None else common.intersection(idx)
    assert common is not None
    out: dict[str, pd.DataFrame] = {}
    dropped: dict[str, int] = {}
    for aid, df in frames.items():
        kept = df[df["open_time"].isin(common)].sort_values("open_time").reset_index(drop=True)
        dropped[aid] = len(df) - len(kept)
        out[aid] = kept
    return PriceSet(out, dropped)


def load_prices(
    sleeve: SleeveConfig,
    data_dir: Path,
    mode: str = "backtest",
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> PriceSet:
    frames: dict[str, pd.DataFrame] = {}
    for asset in sleeve.assets:
        listing = asset.listing_for(mode)
        df = read_listing_bars(listing, sleeve, data_dir)
        if df.empty:
            raise MissingDataError(
                f"no stored {sleeve.decision_bar.value} bars for {listing.instrument_id} "
                f"(asset {asset.id}) -- run `make data` first"
            )
        if start is not None:
            df = df[df["open_time"] >= start]
        if end is not None:
            df = df[df["close_time"] <= end]
        frames[asset.id] = df
    return align(frames)


def read_listing_bars(listing: Listing, sleeve: SleeveConfig, data_dir: Path) -> pd.DataFrame:
    """Stored bars for one listing, its own venue's bars layered over its
    backfill source's (own venue wins where both exist)."""
    own = store.read_bars(
        data_dir,
        venue=listing.venue.value,
        symbol=listing.symbol,
        timeframe=sleeve.decision_bar.value,
    )
    if listing.backfill_venue is None or listing.backfill_symbol is None:
        return own
    backfill = store.read_bars(
        data_dir,
        venue=listing.backfill_venue.value,
        symbol=listing.backfill_symbol,
        timeframe=sleeve.decision_bar.value,
    )
    if backfill.empty:
        return own
    if own.empty:
        return backfill
    backfill = backfill[~backfill["open_time"].isin(own["open_time"])]
    return pd.concat([backfill, own]).sort_values("open_time").reset_index(drop=True)


def load_instruments(
    sleeve: SleeveConfig, mode: str, session: Session | None
) -> tuple[dict[str, Instrument], list[str]]:
    """Instrument metadata per asset id, plus a list of warnings for
    assets with no synced tick/step/min-notional (the engine still runs,
    just without that rounding — the spec's "warn loudly" behaviour)."""
    instruments: dict[str, Instrument] = {}
    warnings: list[str] = []
    for asset in sleeve.assets:
        listing = asset.listing_for(mode)
        record = None
        if session is not None:
            record = session.get(InstrumentRecord, (listing.venue.value, listing.symbol))
        if record is None:
            warnings.append(
                f"no synced metadata for {listing.instrument_id}; run `kestrel data "
                "sync-instruments`. Proceeding without tick/step/min-notional rounding."
            )
            instruments[asset.id] = listing.to_instrument(sleeve.asset_class)
        else:
            instruments[asset.id] = listing.to_instrument(
                sleeve.asset_class, record.tick_size, record.step_size, record.min_notional
            )
    return instruments, warnings


def cost_models(sleeve: SleeveConfig, multiplier: Decimal = Decimal(1)) -> dict[str, CostModel]:
    """Per-asset cost models, optionally scaled (0x/1x/2x/3x cost sensitivity)."""
    out = {}
    for asset in sleeve.assets:
        base = asset.costs.to_cost_model()
        out[asset.id] = CostModel(
            fee_pct=base.fee_pct * multiplier,
            min_fee=base.min_fee * multiplier,
            fixed_fee=base.fixed_fee * multiplier,
            spread_bps=base.spread_bps * multiplier,
            slippage_k=base.slippage_k * multiplier,
            tob_pct=base.tob_pct * multiplier,
            tob_cap=None if base.tob_cap is None else base.tob_cap * multiplier,
        )
    return out


def strategy_params(
    name: str, sleeve: SleeveConfig, overrides: dict[str, Any] | None = None
) -> BaseModel:
    """A strategy's params for this sleeve: sleeve-derived fields
    (``bars_per_day``, ``periods_per_year``, a default ``symbol``) filled
    in, then ``overrides`` (config or a grid point) on top."""
    entry = get_strategy_entry(name)
    fields = entry.params_cls.model_fields
    kwargs: dict[str, Any] = {}
    if "bars_per_day" in fields:
        kwargs["bars_per_day"] = sleeve.bars_per_day
    if "periods_per_year" in fields:
        kwargs["periods_per_year"] = sleeve.periods_per_year
    if "symbol" in fields:
        kwargs["symbol"] = sleeve.regime_benchmark
    if "top_n" in fields:
        kwargs["top_n"] = min(3, max(1, len(sleeve.assets) // 2))
    kwargs.update(overrides or {})
    return entry.params_cls(**kwargs)


def build_strategy(params: BaseModel, name: str) -> tuple[Strategy, int]:
    """(strategy instance, warm-up bars it needs before its first real decision)."""
    entry = get_strategy_entry(name)
    strategy: Strategy = entry.strategy_cls(params=params)
    module = importlib.import_module(entry.strategy_cls.__module__)
    lookback_fn = getattr(module, "required_lookback_bars", None)
    warmup = int(lookback_fn(params)) if lookback_fn else 0
    return strategy, warmup


def vectorized_weights(name: str, params: BaseModel, close_panel: pd.DataFrame) -> pd.DataFrame:
    return get_strategy_entry(name).compute_weights(close_panel, params)
