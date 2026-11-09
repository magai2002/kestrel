"""``kestrel`` command-line entrypoint (typer).

``Settings()`` is constructed exactly once, inside each command
function — never at module import time (see ``config/__init__.py``).
Phase 0 only wires up the ``data`` sub-commands; ``backtest``,
``validate`` (the strategy-validation pipeline, not ``data validate``),
``run``, ``report`` and ``admin`` arrive with the phases that need them.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pandas as pd
import typer

from config.settings import Settings
from config.sleeve import load_sleeve_config
from core.enums import Timeframe, Venue
from data import store, validate
from data.calendars import crypto_expected_bar_starts, nyse_session_dates
from data.sources import alpaca_bars, binance_bulk, ccxt_ohlcv

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


def _crypto_expected_grid(df: pd.DataFrame, timeframe: Timeframe) -> pd.DatetimeIndex:
    start = df["open_time"].min()
    end = df["open_time"].max() + pd.Timedelta(seconds=timeframe.timedelta_seconds)
    return crypto_expected_bar_starts(start, end, timeframe.pandas_freq)


if __name__ == "__main__":
    app()
