"""``kestrel`` command-line entrypoint (typer).

``Settings()`` is constructed exactly once, inside each command
function — never at module import time (see ``config/__init__.py``).
Sub-apps: ``data`` (fetch/validate/sync), ``backtest``, ``validate``
(the strategy-validation pipeline, not ``data validate``), ``run``,
``report`` and ``admin``.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pandas as pd
import typer
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backtest import inputs
from backtest.engine import run_backtest
from config.settings import Settings
from config.sleeve import load_sleeve, load_sleeve_config
from core.enums import Timeframe, Venue
from core.orm import InstrumentRecord
from core.reproducibility import git_sha
from data import store, validate
from data.calendars import crypto_expected_bar_starts, session_schedule
from data.sources import binance_bulk, binance_exchange_info, ccxt_ohlcv, yahoo_chart

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
        for asset in sleeve.assets:
            if asset.research.venue == Venue.BINANCE:
                _fetch_binance_symbol(
                    asset.research.symbol, start_date, end_date, settings.data_dir, client
                )
            if asset.live.venue == Venue.KRAKEN:
                _fetch_kraken_symbol(asset.live.symbol, sleeve.decision_bar, settings.data_dir)


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
    """Backfill adjusted daily bars from Yahoo: the US research ETFs
    and the UCITS twins' warm-up history. No credentials needed."""
    settings = Settings()
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end) if end else datetime.now(UTC).date()
    sleeve = load_sleeve_config(EQUITIES_SLEEVE_PATH)
    now = datetime.now(UTC)
    targets: list[tuple[str, str]] = []  # (yahoo symbol, calendar)
    for asset in sleeve.assets:
        if asset.research.venue == Venue.YAHOO:
            targets.append((asset.research.symbol, asset.research.calendar or "NYSE"))
        if asset.live.backfill_venue == Venue.YAHOO and asset.live.backfill_symbol:
            targets.append((asset.live.backfill_symbol, asset.live.calendar or "LSE"))

    failures = 0
    with httpx.Client(timeout=30.0) as client:
        for symbol, calendar in targets:
            try:
                df = yahoo_chart.fetch_daily_bars(
                    symbol, start_date, end_date, calendar, client, now
                )
            except (yahoo_chart.YahooShapeError, httpx.HTTPError) as exc:
                typer.echo(f"  Yahoo {symbol}: FAILED -- {exc}", err=True)
                failures += 1
                continue
            written = store.write_bars(df, settings.data_dir)
            first = df["open_time"].min().date() if not df.empty else "-"
            typer.echo(
                f"  Yahoo {symbol} ({calendar}): {len(df)} bars from {first}, {len(written)} file(s)"
            )
    if failures:
        raise typer.Exit(code=1)


@data_app.command("validate")
def validate_data() -> None:
    """Print a validation report for every symbol currently in the store."""
    settings = Settings()
    crypto = load_sleeve_config(CRYPTO_SLEEVE_PATH)
    equities = load_sleeve_config(EQUITIES_SLEEVE_PATH)

    any_issues = False
    crypto_listings = [a.research for a in crypto.assets] + [a.live for a in crypto.assets]
    for listing in crypto_listings:
        for timeframe in (Timeframe.H1, Timeframe.H4, Timeframe.D1):
            if listing.venue != Venue.BINANCE and timeframe != crypto.decision_bar:
                continue  # Only Binance's backfill covers every timeframe.
            df = store.read_bars(
                settings.data_dir,
                venue=listing.venue.value,
                symbol=listing.symbol,
                timeframe=timeframe.value,
            )
            if df.empty:
                continue
            expected = _crypto_expected_grid(df, timeframe)
            report = validate.validate_series(
                df, listing.venue.value, listing.symbol, timeframe, expected
            )
            typer.echo(report.format())
            any_issues = any_issues or not report.is_clean

    equity_series: list[tuple[str, str, str]] = []  # (venue, symbol, calendar)
    for asset in equities.assets:
        equity_series.append(
            (asset.research.venue.value, asset.research.symbol, asset.research.calendar or "NYSE")
        )
        if asset.live.backfill_venue and asset.live.backfill_symbol:
            equity_series.append(
                (
                    asset.live.backfill_venue.value,
                    asset.live.backfill_symbol,
                    asset.live.calendar or "LSE",
                )
            )
        equity_series.append(
            (asset.live.venue.value, asset.live.symbol, asset.live.calendar or "LSE")
        )
    for venue, symbol, calendar in equity_series:
        df = store.read_bars(
            settings.data_dir, venue=venue, symbol=symbol, timeframe=equities.decision_bar.value
        )
        if df.empty:
            continue
        schedule = session_schedule(calendar, df["open_time"].min(), df["open_time"].max())
        sessions = pd.DatetimeIndex(schedule.index).tz_localize("UTC").normalize()
        report = validate.validate_equity_series(df, venue, symbol, equities.decision_bar, sessions)
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
    binance_entries = [a.research for a in crypto.assets if a.research.venue == Venue.BINANCE]
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
    try:
        sleeve_config = load_sleeve(sleeve)
        params = inputs.strategy_params(strategy_name, sleeve_config)
        price_set = inputs.load_prices(sleeve_config, settings.data_dir)
    except (FileNotFoundError, KeyError, inputs.MissingDataError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    strategy, warmup_bars = inputs.build_strategy(params, strategy_name)

    engine = create_engine(settings.postgres_dsn)
    with Session(engine) as session:
        instruments, warnings = inputs.load_instruments(sleeve_config, "backtest", session)
        for warning in warnings:
            typer.echo(f"  warning: {warning}", err=True)
        run_id = str(uuid4())
        results = run_backtest(
            strategy=strategy,
            strategy_name=strategy_name,
            sleeve_name=sleeve,
            prices=price_set.bars,
            instruments=instruments,
            timeframe=sleeve_config.decision_bar,
            initial_capital=Decimal(str(sleeve_config.paper_capital)),
            cost_models=inputs.cost_models(sleeve_config),
            risk_limits=sleeve_config.risk.to_risk_limits(),
            periods_per_year=sleeve_config.periods_per_year,
            run_id=run_id,
            buffer_pct=sleeve_config.buffer_pct,
            participation_cap=sleeve_config.participation_cap,
            bars_per_day=sleeve_config.bars_per_day,
            warmup_bars=warmup_bars,
            db_session=session,
            config_for_hash={
                "strategy": strategy_name,
                "sleeve": sleeve,
                "params": params.model_dump(mode="json"),
            },
        )

    typer.echo(results.format_summary())
    typer.echo(f"\nEvent log written to Postgres (run_id={run_id}).")


@app.command("validate")
def validate_cmd(
    strategy_name: str = typer.Argument(..., help="Strategy name, or 'all'"),
    sleeve: str = typer.Option("all", "--sleeve", help="crypto, equities, or all"),
    holdout: bool = typer.Option(
        False,
        "--holdout",
        help="Also evaluate the locked hold-out. Do this ONCE per strategy/sleeve, when final: "
        "every access is recorded and a second one marks the report contaminated.",
    ),
) -> None:
    """Walk-forward validation with DSR, PBO, bootstrap, cost
    sensitivity and per-year/regime tables. Writes
    reports/<run_id>/report.{html,json}."""
    from research import report_html, validation
    from research.ledger import SqlLedger

    settings = Settings()
    vconf = validation.load_validation_config()
    strategies = list(vconf.strategies) if strategy_name == "all" else [strategy_name]
    sleeves = ["crypto", "equities"] if sleeve == "all" else [sleeve]
    for name in strategies:
        if name not in vconf.strategies:
            typer.echo(f"no validation config for {name!r} in config/validation.yaml", err=True)
            raise typer.Exit(code=1)

    engine = create_engine(settings.postgres_dsn)
    summary_path = settings.reports_dir / "summary.json"
    summary: dict[str, object] = (
        json.loads(summary_path.read_text()) if summary_path.exists() else {}
    )
    sha = git_sha()
    for sleeve_name in sleeves:
        sleeve_config = load_sleeve(sleeve_name)
        try:
            price_set = inputs.load_prices(sleeve_config, settings.data_dir)
        except inputs.MissingDataError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
        for name in strategies:
            run_id = str(uuid4())
            with Session(engine) as session:
                ledger = SqlLedger(session)
                if holdout:
                    hs = price_set.close_panel.index[
                        int(len(price_set.close_panel) * (1 - vconf.holdout_fraction))
                    ]
                    prior = ledger.holdout_access_count(name, sleeve_name, hs.to_pydatetime())
                    if prior:
                        typer.echo(
                            f"\n{'!' * 72}\n!! HOLD-OUT ALREADY ACCESSED {prior}x for {name}/{sleeve_name}. "
                            f"This report will be marked CONTAMINATED.\n{'!' * 72}",
                            err=True,
                        )
                report = validation.run_validation(
                    name, sleeve_config, price_set, vconf, ledger, run_id, sha, holdout
                )
            out_dir = settings.reports_dir / run_id
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "report.json").write_text(json.dumps(report, indent=1))
            (out_dir / "report.html").write_text(report_html.render(report))
            h = report["headline"]
            verdict = "SURVIVES" if report["verdict"]["survives"] else "does not survive"
            typer.echo(
                f"{name:>24} / {sleeve_name:<8}  DSR {_fmt(h['dsr'])}  OOS Sharpe {_fmt(h['oos_sharpe'])}"
                f"  (baseline {_fmt(h['baseline_oos_sharpe'])})  PBO {_fmt(h['pbo'])}  -> {verdict}"
                f"   reports/{run_id}/report.html"
            )
            summary[f"{name}/{sleeve_name}"] = {
                "run_id": run_id,
                "headline": h,
                "verdict": report["verdict"],
                "holdout": {
                    k: report["holdout"].get(k)
                    for k in ("evaluated", "contaminated", "prior_accesses", "start", "end")
                }
                | {"sharpe": (report["holdout"].get("summary") or {}).get("sharpe")},
                "oos": {
                    k: report["oos"][k]
                    for k in (
                        "cagr",
                        "sharpe",
                        "max_drawdown",
                        "turnover_annual",
                        "cost_drag_annual",
                    )
                },
                "generated_at": report["generated_at"],
            }
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=1, sort_keys=True))


def _fmt(x: object) -> str:
    return "  —  " if x is None else f"{float(x):5.2f}"  # type: ignore[arg-type]


def _crypto_expected_grid(df: pd.DataFrame, timeframe: Timeframe) -> pd.DatetimeIndex:
    start = df["open_time"].min()
    end = df["open_time"].max() + pd.Timedelta(seconds=timeframe.timedelta_seconds)
    return crypto_expected_bar_starts(start, end, timeframe.pandas_freq)


if __name__ == "__main__":
    app()
