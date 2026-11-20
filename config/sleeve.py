"""Pydantic schema and loader for per-sleeve YAML configs (Section 3.1).

**A sleeve is a set of logical assets, each listed in up to three
places** — this is the "single cross-venue logical instrument identity"
Phase 0 deferred:

- ``research``: where deep history comes from (backtest/validate).
  Crypto: Binance bulk dumps. Equities: the *US* ETF on Yahoo (``SPY``).
- ``live``: what ``sim`` reads prices from and ``live`` trades.
  Crypto: Kraken. Equities: the UCITS twin on IBKR (``VUSD`` on LSE) —
  EU retail clients can't buy US-domiciled ETFs (no PRIIPs KID), so the
  backtest is researched on the US fund and traded on its Irish twin
  (``docs/adr/0004-ibkr-ucits-hybrid-data.md``).
- ``sandbox``: what ``sandbox`` trades (Binance Spot Testnet for
  crypto; IBKR paper for equities, which is the ``live`` listing on a
  paper account, so it defaults to ``live``).

Strategies key everything by the asset ``id`` (``BTC``, ``SPY``), never
by a venue-specific instrument id, so the same strategy params work in
every mode. For equities the id is the *research* ticker on purpose:
``SPY`` in a live log line means "the S&P 500 sleeve slot", currently
traded as VUSD.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, model_validator

from core.enums import AssetClass, Timeframe, Venue
from core.instrument import Instrument
from execution.costs import CostModel
from risk.limits import GroupCap, RiskLimits


class Listing(BaseModel):
    """One asset on one venue."""

    model_config = ConfigDict(frozen=True)

    venue: Venue
    symbol: str
    base_currency: str
    quote_currency: str
    # IBKR only: the primary exchange (e.g. LSEETF) and currency of the
    # contract to resolve. Ignored for crypto.
    exchange: str | None = None
    # Where warm-up/backfill history for *this* listing comes from when
    # its own venue can't supply years of it (e.g. a UCITS twin's
    # history from Yahoo, VUSD.L). None = the venue itself.
    backfill_venue: Venue | None = None
    backfill_symbol: str | None = None
    # pandas_market_calendars name for session times (NYSE, LSE). None = 24/7.
    calendar: str | None = None

    @property
    def instrument_id(self) -> str:
        return f"{self.venue.value}:{self.symbol}"

    def to_instrument(
        self,
        asset_class: AssetClass,
        tick_size: Decimal | None = None,
        step_size: Decimal | None = None,
        min_notional: Decimal | None = None,
    ) -> Instrument:
        return Instrument(
            venue=self.venue,
            symbol=self.symbol,
            asset_class=asset_class,
            base_currency=self.base_currency,
            quote_currency=self.quote_currency,
            tick_size=tick_size,
            step_size=step_size,
            min_notional=min_notional,
        )


class CostConfig(BaseModel):
    """Per-asset cost assumptions; becomes an ``execution.costs.CostModel``.

    ``notes`` is free text that ends up in the validation report, so an
    unverified assumption (e.g. a TOB rate) is visible wherever its
    consequences are.
    """

    model_config = ConfigDict(frozen=True)

    fee_pct: Decimal = Decimal("0")
    min_fee: Decimal = Decimal("0")
    fixed_fee: Decimal = Decimal("0")
    spread_bps: Decimal = Decimal("2")
    slippage_k: Decimal = Decimal("0.1")
    tob_pct: Decimal = Decimal("0")
    tob_cap: Decimal | None = None
    notes: str = ""

    def to_cost_model(self) -> CostModel:
        return CostModel(
            fee_pct=self.fee_pct,
            min_fee=self.min_fee,
            fixed_fee=self.fixed_fee,
            spread_bps=self.spread_bps,
            slippage_k=self.slippage_k,
            tob_pct=self.tob_pct,
            tob_cap=self.tob_cap,
        )


class AssetConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    research: Listing
    live: Listing
    sandbox: Listing | None = None
    costs: CostConfig = Field(default_factory=CostConfig)

    def listing_for(self, mode: str) -> Listing:
        if mode == "backtest":
            return self.research
        if mode == "sandbox":
            return self.sandbox or self.live
        if mode in ("sim", "live"):
            return self.live
        raise ValueError(f"unknown mode {mode!r}")


class GroupCapConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    assets: tuple[str, ...]
    cap: float


class RiskConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    per_instrument_weight_cap: float = 0.30
    group_caps: tuple[GroupCapConfig, ...] = ()
    gross_exposure_cap: float = 1.0
    max_order_notional_pct: float = 0.30
    price_band_pct: float = 0.02
    daily_loss_limit_pct: float = 0.03
    drawdown_limit_pct: float = 0.20
    # Live-only kill switches (Section 6.3); ignored by backtests.
    stale_data_bars: float = 2.0
    reconcile_tolerance_pct: float = 0.01
    error_burst_count: int = 5
    error_burst_window_minutes: int = 15
    clock_drift_seconds: float = 5.0
    flatten_on_trip: bool = False  # ADR 0006: default is cancel-and-hold

    def to_risk_limits(self) -> RiskLimits:
        return RiskLimits(
            per_instrument_weight_cap=self.per_instrument_weight_cap,
            group_caps=tuple(
                GroupCap(name=g.name, instrument_ids=g.assets, cap=g.cap) for g in self.group_caps
            ),
            gross_exposure_cap=self.gross_exposure_cap,
            max_order_notional_pct=self.max_order_notional_pct,
            price_band_pct=self.price_band_pct,
            daily_loss_limit_pct=self.daily_loss_limit_pct,
            drawdown_limit_pct=self.drawdown_limit_pct,
        )


class StrategyAllocation(BaseModel):
    """One strategy's share of the sleeve (spec Section 5: "overlay of sleeves")."""

    model_config = ConfigDict(frozen=True)

    name: str
    weight: float = Field(gt=0, le=1)
    params: dict[str, object] = Field(default_factory=dict)


class LiveGateConfig(BaseModel):
    """Phase 7 per-sleeve gates (spec Section 13). Never defaulted to "on"."""

    model_config = ConfigDict(frozen=True)

    i_understand_this_uses_real_money: bool = False
    max_live_capital: Decimal | None = None
    daily_loss_limit_abs: Decimal | None = None
    min_sim_weeks: float = 8.0
    max_tracking_gap_pct: float = 2.0  # annualised % gap, sim vs shadow backtest
    min_dsr: float = 0.95
    keys_trade_only_confirmed: bool = False
    emergency_stop_rehearsed: bool = False


class SleeveConfig(BaseModel):
    """One sleeve: its own account, capital, universe and decision bar."""

    model_config = ConfigDict(frozen=True)

    name: str
    asset_class: AssetClass
    decision_bar: Timeframe
    base_currency: str
    paper_capital: PositiveFloat
    periods_per_year_days: float  # 365 crypto, 252 equities (the annualisation trap)
    buffer_pct: float = 0.10
    participation_cap: Decimal = Decimal("0.05")
    # How long after a bar's close the runner waits before acting, so
    # the venue has published the closed bar (spec Section 12, Phase 4).
    bar_close_delay_seconds: int = 30
    regime_benchmark: str  # asset id whose 200-day trend defines bull/bear/sideways
    assets: list[AssetConfig]
    risk: RiskConfig = Field(default_factory=RiskConfig)
    strategies: list[StrategyAllocation] = Field(default_factory=list)
    live_gates: LiveGateConfig = Field(default_factory=LiveGateConfig)

    @model_validator(mode="after")
    def _consistent(self) -> SleeveConfig:
        ids = [a.id for a in self.assets]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate asset ids in sleeve {self.name!r}: {ids}")
        if self.regime_benchmark not in ids:
            raise ValueError(f"regime_benchmark {self.regime_benchmark!r} is not an asset id")
        for group in self.risk.group_caps:
            unknown = set(group.assets) - set(ids)
            if unknown:
                raise ValueError(f"group cap {group.name!r} names unknown assets {unknown}")
        if self.strategies:
            total = sum(s.weight for s in self.strategies)
            if abs(total - 1.0) > 1e-9:
                raise ValueError(f"strategy weights must sum to 1, got {total}")
        return self

    @property
    def asset_ids(self) -> list[str]:
        return [a.id for a in self.assets]

    def asset(self, asset_id: str) -> AssetConfig:
        for a in self.assets:
            if a.id == asset_id:
                return a
        raise KeyError(asset_id)

    @property
    def bars_per_day(self) -> float:
        return Timeframe.D1.timedelta_seconds / self.decision_bar.timedelta_seconds

    @property
    def periods_per_year(self) -> float:
        return self.periods_per_year_days * self.bars_per_day


def load_sleeve_config(path: Path) -> SleeveConfig:
    """Load and validate a sleeve YAML file.

    Raises ``pydantic.ValidationError`` with a precise field-level
    message if the file is malformed — fail loudly rather than silently
    coercing, per the spec's lesson about free APIs/config changing
    shape underneath you.
    """
    raw = yaml.safe_load(path.read_text())
    return SleeveConfig.model_validate(raw)


SLEEVE_DIR = Path("config/sleeves")


def load_sleeve(name: str, sleeve_dir: Path = SLEEVE_DIR) -> SleeveConfig:
    path = sleeve_dir / f"{name}.yaml"
    if not path.exists():
        available = sorted(p.stem for p in sleeve_dir.glob("*.yaml"))
        raise FileNotFoundError(f"no sleeve config {path}; available: {available}")
    return load_sleeve_config(path)
