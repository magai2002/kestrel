"""Assemble a ``SleeveRuntime`` for one (mode, sleeve) from config and
``Settings`` — the only place that knows which feed and broker each
mode uses (spec Section 3.2's mode table, as amended by ADR 0004):

| mode    | crypto feed / broker            | equities feed / broker            |
| ------- | ------------------------------- | --------------------------------- |
| sim     | Kraken public OHLC / SimBroker  | Yahoo (twins) or IBKR / SimBroker |
| sandbox | Binance testnet / testnet       | IBKR (paper) / IBKR paper         |
| live    | Kraken / Kraken  — Phase 7 gate | IBKR / IBKR live — Phase 7 gate   |
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import ccxt
import httpx
from sqlalchemy.orm import Session

from backtest import inputs
from config.settings import Settings
from config.sleeve import SleeveConfig, load_sleeve
from core.enums import Venue
from execution.brokers import binance_testnet, ibkr, kraken
from execution.brokers.base import LiveBroker
from execution.live_execution import ExecutionPolicy
from live import gates
from live.feeds import CcxtFeed, Feed, IbkrFeed, YahooFeed
from live.sleeve_runner import SleeveRuntime
from live.state import StateStore
from observability.alerts import Alerter
from strategies.base import Strategy
from strategies.overlay import STATEFUL, CombinedStrategy

MODES = ("sim", "sandbox", "live")


def build_strategy(sleeve: SleeveConfig) -> tuple[Strategy, str]:
    if not sleeve.strategies:
        raise ValueError(f"sleeve {sleeve.name!r} has no strategies configured")
    components: list[tuple[float, Strategy]] = []
    for alloc in sleeve.strategies:
        if alloc.name in STATEFUL:
            raise ValueError(
                f"{alloc.name!r} keeps incremental state that a restart would lose; it can't run live"
            )
        params = inputs.strategy_params(alloc.name, sleeve, dict(alloc.params))
        strategy, _ = inputs.build_strategy(params, alloc.name)
        components.append((alloc.weight, strategy))
    if len(components) == 1:
        return components[0][1], sleeve.strategies[0].name
    return CombinedStrategy(components), "portfolio"


def _ccxt_symbols(sleeve: SleeveConfig, mode: str) -> dict[str, str]:
    out = {}
    for a in sleeve.assets:
        listing = a.listing_for(mode)
        out[listing.symbol] = f"{listing.base_currency}/{listing.quote_currency}"
    return out


def _ibkr_listings(sleeve: SleeveConfig, mode: str) -> dict[str, tuple[str, str]]:
    return {
        a.listing_for(mode).symbol: (
            a.listing_for(mode).exchange or "SMART",
            a.listing_for(mode).quote_currency,
        )
        for a in sleeve.assets
    }


def build_runtime(
    mode: str,
    sleeve_name: str,
    settings: Settings,
    store: StateStore,
    session: Session | None,
    alerter: Alerter,
    run_id: str,
    sleep: Callable[[float], None] = time.sleep,
    elapsed: Callable[[], float] = time.monotonic,
    ib_connection: Any | None = None,
) -> SleeveRuntime:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
    if mode == "live":
        # Every spec Section 13 gate, or nothing. Nothing below this line
        # can construct a live broker without passing it.
        gates.require_live_unlocked(settings=settings, sleeve=sleeve_name, session=session)

    sleeve = load_sleeve(sleeve_name)
    strategy, label = build_strategy(sleeve)
    instruments, warnings = inputs.load_instruments(sleeve, mode, session)
    venues = {a.listing_for(mode).venue for a in sleeve.assets}
    if len(venues) != 1:
        raise ValueError(f"sleeve {sleeve_name} mixes venues {venues} in mode {mode}")
    venue = venues.pop()

    feed: Feed
    broker: LiveBroker | None = None
    ib = ib_connection
    if venue in (Venue.KRAKEN, Venue.BINANCE_TESTNET):
        if venue == Venue.KRAKEN:
            ex = ccxt.kraken({"enableRateLimit": True})
        else:
            ex = binance_testnet.make_exchange()
        feed = CcxtFeed(ex, venue, _ccxt_symbols(sleeve, mode))
        if mode == "sandbox":
            if not (settings.binance_testnet_api_key and settings.binance_testnet_api_secret):
                raise RuntimeError(
                    "sandbox crypto needs KESTREL_BINANCE_TESTNET_API_KEY/_SECRET "
                    "(create them at testnet.binance.vision, GitHub login)"
                )
            broker = binance_testnet.make_broker(
                _ccxt_symbols(sleeve, mode),
                settings.binance_testnet_api_key.get_secret_value(),
                settings.binance_testnet_api_secret.get_secret_value(),
            )
        elif mode == "live":
            if not (settings.kraken_api_key and settings.kraken_api_secret):
                raise RuntimeError("live crypto needs KESTREL_KRAKEN_API_KEY/_SECRET (trade-only)")
            broker = kraken.make_broker(
                _ccxt_symbols(sleeve, mode),
                settings.kraken_api_key.get_secret_value(),
                settings.kraken_api_secret.get_secret_value(),
            )
    elif venue == Venue.IBKR:
        needs_gateway = mode != "sim" or sleeve.sim_feed == "ibkr"
        if needs_gateway and ib is None:
            port = settings.ibkr_paper_port if mode != "live" else settings.ibkr_live_port
            account = settings.ibkr_paper_account if mode != "live" else settings.ibkr_live_account
            ib = ibkr.connect(
                settings.ibkr_host,
                port,
                settings.ibkr_client_id + MODES.index(mode),
                account.get_secret_value() if account else "",
            )
        contracts = (
            ibkr.qualify_contracts(ib, _ibkr_listings(sleeve, mode)) if ib is not None else {}
        )
        if mode == "sim" and sleeve.sim_feed == "yahoo":
            feed = YahooFeed(httpx.Client(timeout=30))
        else:
            feed = IbkrFeed(ib, contracts)
        if mode != "sim":
            account = (
                settings.ibkr_paper_account if mode == "sandbox" else settings.ibkr_live_account
            )
            broker = ibkr.IbkrBroker(
                ib,
                contracts,
                account.get_secret_value() if account else "",
                currency=sleeve.base_currency,
            )
    else:
        raise ValueError(f"no live feed for venue {venue}")

    ex_conf = sleeve.execution
    rt = SleeveRuntime(
        mode=mode,
        sleeve=sleeve,
        strategy=strategy,
        strategy_label=label,
        feed=feed,
        store=store,
        alerter=alerter,
        data_dir=settings.data_dir,
        instruments=instruments,
        cost_models=inputs.cost_models(sleeve),
        run_id=run_id,
        broker=broker,
        policy=ExecutionPolicy(
            ex_conf.protection_bps,
            ex_conf.timeout_seconds,
            ex_conf.poll_seconds,
            ex_conf.max_requotes,
        ),
        sleep=sleep,
        elapsed=elapsed,
        quote_currency=next(iter(sleeve.assets)).listing_for(mode).quote_currency,
        # Gate 2: live sizes from a ledger seeded with max_live_capital and
        # never from more than it, whatever the venue balance says.
        capital_cap=sleeve.live_gates.max_live_capital if mode == "live" else None,
        daily_loss_limit_abs=sleeve.live_gates.daily_loss_limit_abs if mode == "live" else None,
    )
    for w in warnings:
        rt.alerter.send(
            f"[{mode}/{sleeve_name}] missing instrument metadata",
            w,
            f"{mode}/{sleeve_name}/meta",
            "low",
        )
    return rt
