"""``kestrel`` command-line entrypoint (typer).

``Settings()`` is constructed exactly once, inside each command
function — never at module import time (see ``config/__init__.py``).
Phase 0 only wires up the ``data`` sub-commands; ``backtest``,
``validate`` (the strategy-validation pipeline, not ``data validate``),
``run``, ``report`` and ``admin`` arrive with the phases that need them.
"""

from __future__ import annotations

import importlib
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pandas as pd
import typer
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backtest.engine import run_backtest
from config.settings import Settings
from config.sleeve import load_sleeve_config
from core.enums import Timeframe, Venue
from core.instrument import Instrument
from core.orm import InstrumentRecord
from data import store, validate
from data.calendars import crypto_expected_bar_starts, nyse_session_dates
from data.sources import alpaca_bars, binance_bulk, binance_exchange_info, ccxt_ohlcv
from execution.costs import CostModel
from risk.limits import RiskLimits
from strategies.registry import get as get_strategy_entry

app = typer.Typer(help="kestrel: multi-market systematic trading bot")
data_app = typer.Typer(help="Fetch, store and validate market data")
app.add_typer(data_app, name="data")

CRYPTO_SLEEVE_PATH = Path("config/sleeves/crypto.yaml")
EQUITIES_SLEEVE_PATH = Path("config/sleeves/equities.yaml")
DEFAULT_CRYPTO_START = date(2019, 1, 1)
DEFAULT_EQUITIES_START = date(2016, 1, 1)


@data_app.command("fetch-crypto")
def fetch_crypto(
    start: str = typer.Option(
        DEFAULT_CRYPTO_START.isoformat(), help="Backfill start, YYYY-MM-DD (UTC)"
    ),
    end: str | None = typer.Option(None, help="Backfill end, YYYY-MM-DD (UTC); defaults to today"),
) -> None:
    """Backfill crypto history from Binance (1h, resampled to 4h/1d) and
    a recent Kraken cross-check window."""
    settings = Settings()
    sleeve = load_sleeve_config(CRYPTO_SLEEVE_PATH)
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end) if end else datetime.now(UTC).date()

    with httpx.Client(timeout=30.0) as client:
        for entry in sleeve.universe:
            if entry.venue == Venue.BINANCE:
                _fetch_binance_symbol(entry.symbol, start_date, end_date, settings.data_dir, client)
            elif entry.venue == Venue.KRAKEN:
                _fetch_kraken_symbol(entry.symbol, sleeve.decision_bar, settings.data_dir)


def _fetch_binance_symbol(
    symbol: str, start: date, end: date, data_dir: Path, client: httpx.Client
) -> None:
    typer.echo(f"Binance: backfilling {symbol} 1h, {start} .. {end}")
    hourly = binance_bulk.backfill_klines(symbol, Timeframe.H1.value, start, end, client=client)
    if hourly.empty:
        typer.echo(f"  no data returned for {symbol} (see log output above for skipped months)")
        return
    written = store.write_bars(hourly, data_dir)
    typer.echo(f"  wrote {len(written)} partition file(s) at 1h ({len(hourly)} bars)")

    for target in (Timeframe.H4, Timeframe.D1):
        resampled = binance_bulk.resample(hourly, target, Timeframe.H1.timedelta_seconds)
        if resampled.empty:
            continue
        written = store.write_bars(resampled, data_dir)
        typer.echo(
            f"  wrote {len(written)} partition file(s) at {target.value} ({len(resampled)} bars)"
        )


def _fetch_kraken_symbol(symbol: str, decision_bar: Timeframe, data_dir: Path) -> None:
    typer.echo(
        f"Kraken: fetching recent {symbol} {decision_bar.value} (cross-check window, max 720 candles)"
    )
    df = ccxt_ohlcv.fetch_recent_ohlcv(symbol, decision_bar)
    if df.empty:
        typer.echo(f"  no data returned for {symbol}")
        return
    written = store.write_bars(df, data_dir)
    typer.echo(f"  wrote {len(written)} partition file(s) ({len(df)} bars)")


@data_app.command("fetch-equities")
def fetch_equities(
    start: str = typer.Option(
        DEFAULT_EQUITIES_START.isoformat(), help="Backfill start, YYYY-MM-DD (UTC)"
    ),
    end: str | None = typer.Option(None, help="Backfill end, YYYY-MM-DD (UTC); defaults to today"),
) -> None:
    """Backfill adjusted daily ETF bars from Alpaca. Requires
    ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY (free paper-account keys;
    see .env.example)."""
    settings = Settings()
    if settings.alpaca_api_key_id is None or settings.alpaca_api_secret_key is None:
        typer.echo(
            "Alpaca credentials not set. Add KESTREL_ALPACA_API_KEY_ID and "
            "KESTREL_ALPACA_API_SECRET_KEY to .env (see .env.example) — a free "
            "paper-account key pair is enough, no live-trading access needed.",
            err=True,
        )
        raise typer.Exit(code=1)

    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end) if end else datetime.now(UTC).date()
    sleeve = load_sleeve_config(EQUITIES_SLEEVE_PATH)
    symbols = [e.symbol for e in sleeve.universe if e.venue == Venue.ALPACA]
    client = alpaca_bars.make_client(
        settings.alpaca_api_key_id.get_secret_value(),
        settings.alpaca_api_secret_key.get_secret_value(),
    )
    typer.echo(f"Alpaca: backfilling {len(symbols)} symbols, {start_date} .. {end_date}")
    df = alpaca_bars.fetch_daily_bars(symbols, start_date, end_date, client)
    if df.empty:
        typer.echo("  no data returned", err=True)
        raise typer.Exit(code=1)
    written = store.write_bars(df, settings.data_dir)
    typer.echo(
        f"  wrote {len(written)} partition file(s) ({len(df)} bars across {df['symbol'].nunique()} symbols)"
    )


@data_app.command("validate")
def validate_data() -> None:
    """Print a validation report for every symbol currently in the store."""
    settings = Settings()
    crypto = load_sleeve_config(CRYPTO_SLEEVE_PATH)
    equities = load_sleeve_config(EQUITIES_SLEEVE_PATH)

    any_issues = False
    for entry in crypto.universe:
        for timeframe in (Timeframe.H1, Timeframe.H4, Timeframe.D1):
            if entry.venue == Venue.KRAKEN and timeframe != crypto.decision_bar:
                continue  # Kraken fetch only ever covers the decision bar.
            df = store.read_bars(
                settings.data_dir,
                venue=entry.venue.value,
                symbol=entry.symbol,
                timeframe=timeframe.value,
            )
            if df.empty:
                continue
            expected = _crypto_expected_grid(df, timeframe)
            report = validate.validate_series(
                df, entry.venue.value, entry.symbol, timeframe, expected
            )
            typer.echo(report.format())
            any_issues = any_issues or not report.is_clean

    for entry in equities.universe:
        df = store.read_bars(
            settings.data_dir,
            venue=entry.venue.value,
            symbol=entry.symbol,
            timeframe=equities.decision_bar.value,
        )
        if df.empty:
            continue
        sessions = nyse_session_dates(df["open_time"].min(), df["open_time"].max())
        report = validate.validate_equity_series(
            df, entry.venue.value, entry.symbol, equities.decision_bar, sessions
        )
        typer.echo(report.format())
        any_issues = any_issues or not report.is_clean

    if any_issues:
        typer.echo(
            "\nValidation found issues above. Known/expected gaps belong in docs/progress.md."
        )
        raise typer.Exit(code=1)
    typer.echo("\nAll series clean.")


@data_app.command("sync-instruments")
def sync_instruments() -> None:
    """Fetch tick size, step size and minimum notional for every
    Binance-venue instrument in the crypto sleeve and upsert them into
    the ``instruments`` table (design rule 4's rounding needs real
    values, not the nullable placeholders Phase 0 left)."""
    settings = Settings()
    crypto = load_sleeve_config(CRYPTO_SLEEVE_PATH)
    binance_entries = [e for e in crypto.universe if e.venue == Venue.BINANCE]
    if not binance_entries:
        typer.echo("no Binance-venue instruments in the crypto sleeve config")
        return

    filters = binance_exchange_info.fetch_filters([e.symbol for e in binance_entries])
    engine = create_engine(settings.postgres_dsn)
    with Session(engine) as session:
        for entry in binance_entries:
            found = filters.get(entry.symbol)
            if found is None:
                typer.echo(f"  no exchange info returned for {entry.symbol}, skipping", err=True)
                continue
            record = session.get(InstrumentRecord, (entry.venue.value, entry.symbol))
            if record is None:
                record = InstrumentRecord(
                    venue=entry.venue.value,
                    symbol=entry.symbol,
                    asset_class=crypto.asset_class.value,
                    base_currency=entry.base_currency,
                    quote_currency=entry.quote_currency,
                )
                session.add(record)
            record.tick_size = found.tick_size
            record.step_size = found.step_size
            record.min_notional = found.min_notional
            typer.echo(
                f"  synced {entry.venue.value}:{entry.symbol}: tick={found.tick_size} "
                f"step={found.step_size} min_notional={found.min_notional}"
            )
        session.commit()


@app.command("backtest")
def backtest_cmd(
    strategy_name: str = typer.Argument(..., help="Strategy name, e.g. trend_vol_target"),
    sleeve: str = typer.Option(..., "--sleeve", help="Sleeve name: crypto or equities"),
) -> None:
    """Run the event-driven backtest engine on stored historical data
    and print a results summary (Phase 2's "done when": prints a
    results summary and writes the event log)."""
    settings = Settings()
    if sleeve == "crypto":
        sleeve_path, primary_venue = CRYPTO_SLEEVE_PATH, Venue.BINANCE
    elif sleeve == "equities":
        sleeve_path, primary_venue = EQUITIES_SLEEVE_PATH, Venue.ALPACA
    else:
        typer.echo(f"unknown sleeve {sleeve!r} (expected crypto or equities)", err=True)
        raise typer.Exit(code=1)
    sleeve_config = load_sleeve_config(sleeve_path)

    try:
        entry = get_strategy_entry(strategy_name)
    except KeyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    # Only the sleeve's primary (historical-backfill) venue participates
    # in a backtest -- e.g. crypto's Kraken entries are a forward-looking
    # live/sim cross-check window (Section 3.3), not backtest history.
    universe = [e for e in sleeve_config.universe if e.venue == primary_venue]
    if not universe:
        typer.echo(f"no {primary_venue.value}-venue instruments in sleeve {sleeve!r}", err=True)
        raise typer.Exit(code=1)

    prices: dict[str, pd.DataFrame] = {}
    for u in universe:
        df = store.read_bars(
            settings.data_dir,
            venue=primary_venue.value,
            symbol=u.symbol,
            timeframe=sleeve_config.decision_bar.value,
        )
        if df.empty:
            typer.echo(
                f"no stored data for {primary_venue.value}:{u.symbol} at "
                f"{sleeve_config.decision_bar.value} -- run `make data` first",
                err=True,
            )
            raise typer.Exit(code=1)
        prices[f"{primary_venue.value}:{u.symbol}"] = df

    bars_per_day = Timeframe.D1.timedelta_seconds / sleeve_config.decision_bar.timedelta_seconds
    if sleeve == "crypto":
        periods_per_year = 365.0 * bars_per_day
        cost_model_kwargs: dict[str, Decimal] = dict(
            fee_pct=Decimal("0.008"), spread_bps=Decimal("2"), slippage_k=Decimal("0.1")
        )
        risk_limits = RiskLimits(
            per_instrument_weight_cap=0.60, max_order_notional_pct=0.30, price_band_pct=0.02
        )
    else:
        periods_per_year = 252.0 * bars_per_day
        cost_model_kwargs = dict(
            fee_pct=Decimal("0"),
            spread_bps=Decimal("1"),
            slippage_k=Decimal("0.1"),
            tob_pct=Decimal("0.0012"),
            tob_cap=Decimal("1600"),
        )
        risk_limits = RiskLimits(
            per_instrument_weight_cap=0.30, max_order_notional_pct=0.30, price_band_pct=0.01
        )
    cost_models = {iid: CostModel(**cost_model_kwargs) for iid in prices}

    engine = create_engine(settings.postgres_dsn)
    instruments: dict[str, Instrument] = {}
    with Session(engine) as session:
        for u in universe:
            instrument_id = f"{primary_venue.value}:{u.symbol}"
            record = session.get(InstrumentRecord, (primary_venue.value, u.symbol))
            if record is None:
                typer.echo(
                    f"  warning: no synced metadata for {instrument_id}; run "
                    "`kestrel data sync-instruments`. Proceeding without tick/step/"
                    "min-notional rounding for it.",
                    err=True,
                )
                instruments[instrument_id] = Instrument(
                    venue=primary_venue,
                    symbol=u.symbol,
                    asset_class=sleeve_config.asset_class,
                    base_currency=u.base_currency,
                    quote_currency=u.quote_currency,
                )
            else:
                instruments[instrument_id] = Instrument(
                    venue=primary_venue,
                    symbol=u.symbol,
                    asset_class=sleeve_config.asset_class,
                    base_currency=u.base_currency,
                    quote_currency=u.quote_currency,
                    tick_size=record.tick_size,
                    step_size=record.step_size,
                    min_notional=record.min_notional,
                )

        extra_param_kwargs = {"bars_per_day": bars_per_day, "periods_per_year": periods_per_year}
        valid_kwargs = {
            k: v for k, v in extra_param_kwargs.items() if k in entry.params_cls.model_fields
        }
        strategy_params = entry.params_cls(**valid_kwargs)
        strategy = entry.strategy_cls(params=strategy_params)

        strategy_module = importlib.import_module(entry.strategy_cls.__module__)
        lookback_fn = getattr(strategy_module, "required_lookback_bars", None)
        warmup_bars = lookback_fn(strategy_params) if lookback_fn else 0

        run_id = str(uuid4())
        results = run_backtest(
            strategy=strategy,
            strategy_name=strategy_name,
            sleeve_name=sleeve,
            prices=prices,
            instruments=instruments,
            timeframe=sleeve_config.decision_bar,
            initial_capital=Decimal(str(sleeve_config.paper_capital)),
            cost_models=cost_models,
            risk_limits=risk_limits,
            periods_per_year=periods_per_year,
            run_id=run_id,
            bars_per_day=bars_per_day,
            warmup_bars=warmup_bars,
            db_session=session,
            config_for_hash={
                "strategy": strategy_name,
                "sleeve": sleeve,
                "params": strategy_params.model_dump(mode="json"),
            },
        )

    typer.echo(results.format_summary())
    typer.echo(f"\nEvent log written to Postgres (run_id={run_id}).")


def _crypto_expected_grid(df: pd.DataFrame, timeframe: Timeframe) -> pd.DatetimeIndex:
    start = df["open_time"].min()
    end = df["open_time"].max() + pd.Timedelta(seconds=timeframe.timedelta_seconds)
    return crypto_expected_bar_starts(start, end, timeframe.pandas_freq)


if __name__ == "__main__":
    app()
