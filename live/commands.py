"""CLI commands for the live side: ``kestrel run``, ``kestrel admin ...``,
``kestrel ibkr ...``. Registered on the main app in ``cli.py``.

The admin commands talk to the database the runner reads every cycle,
not to the runner process: a reset, pause or rebaseline takes effect at
the runner's next cycle. That's the same boundary the admin API router
uses (Phase 6, ADR 0006): nothing admin-ish is reachable from the
public internet.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import typer
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from config.settings import Settings
from config.sleeve import load_sleeve
from core.clock import WallClock
from core.orm import InstrumentRecord, KillSwitchRecord, SleeveStateRecord
from observability import logging as obs_logging
from observability.alerts import Alerter, NtfyAlerter, RecordingAlerter
from risk.kill_switch import KILL_SWITCH_NAMES

admin_app = typer.Typer(help="Operate a running bot: status, pause/resume, reset kill switches")
ibkr_app = typer.Typer(help="Interactive Brokers helpers (needs a running IB Gateway)")
SLEEVES = ("crypto", "equities")


def _alerter(settings: Settings) -> Alerter:
    if settings.ntfy_url:
        token = settings.ntfy_token.get_secret_value() if settings.ntfy_token else None
        return NtfyAlerter(settings.ntfy_url, settings.ntfy_topic, token)
    return RecordingAlerter()


def run_cmd(
    mode: str = typer.Option(..., "--mode", help="sim | sandbox (live is Phase 7-gated)"),
    sleeve: str = typer.Option("all", "--sleeve", help="crypto, equities or all"),
    once: bool = typer.Option(False, "--once", help="Run one catch-up cycle per sleeve and exit"),
    metrics: bool = typer.Option(True, "--metrics/--no-metrics", help="Serve Prometheus metrics"),
) -> None:
    """Run the live engine: wait for each bar close, decide, trade, reconcile."""
    from live.build import build_runtime
    from live.runner import install_signal_handlers, run_forever, run_once
    from live.state import SqlStateStore

    settings = Settings()
    obs_logging.configure(settings.log_level, settings.log_json)
    sleeves = list(SLEEVES) if sleeve == "all" else [sleeve]
    engine = create_engine(settings.postgres_dsn, pool_pre_ping=True)
    session = Session(engine)
    store = SqlStateStore(session)
    alerter = _alerter(settings)
    run_id = f"{mode}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:6]}"

    hooks = None
    if metrics and not once:
        from observability import metrics as obs_metrics

        hooks = obs_metrics.start(settings.metrics_port, settings.metrics_addr)

    runtimes = []
    for name in sleeves:
        try:
            rt = build_runtime(mode, name, settings, store, session, alerter, run_id)
        except Exception as exc:
            typer.echo(f"could not start {mode}/{name}: {exc}", err=True)
            alerter.send(
                f"[{mode}/{name}] failed to start", str(exc), f"{mode}/{name}/start", "high"
            )
            raise typer.Exit(code=1) from exc
        if hooks is not None:
            rt.on_cycle = hooks.on_cycle
        runtimes.append(rt)

    clock = WallClock()
    if once:
        for rt in runtimes:
            result = run_once(rt, clock.now())
            typer.echo(
                f"{mode}/{rt.name}: {result.status} bar={result.bar_close} equity={result.equity} "
                f"orders={result.orders} fills={result.fills} tripped={result.tripped} {result.detail}"
            )
        return
    stop = threading.Event()
    install_signal_handlers(stop)
    run_forever(runtimes, clock, stop, heartbeat=hooks.heartbeat if hooks else None)


@admin_app.command("status")
def status() -> None:
    """Per (mode, sleeve): heartbeat, last bar, equity, paused, kill switches."""
    from live.state import SleeveState

    settings = Settings()
    with Session(create_engine(settings.postgres_dsn)) as s:
        rows = s.scalars(
            select(SleeveStateRecord).order_by(SleeveStateRecord.mode, SleeveStateRecord.sleeve)
        ).all()
        if not rows:
            typer.echo("no runner state yet (start one with `kestrel run --mode sim`)")
        for r in rows:
            st = SleeveState.from_json(r.state)
            switches = s.scalars(
                select(KillSwitchRecord).where(
                    KillSwitchRecord.mode == r.mode,
                    KillSwitchRecord.sleeve == r.sleeve,
                    KillSwitchRecord.tripped,
                )
            ).all()
            trips = ", ".join(f"{k.name} ({k.reason})" for k in switches) or "none"
            pos = ", ".join(f"{a}={q.normalize()}" for a, q in st.positions.items() if q) or "flat"
            typer.echo(
                f"{r.mode:>8}/{r.sleeve:<9} heartbeat={r.heartbeat_at:%Y-%m-%d %H:%M}Z "
                f"last_bar={st.last_bar_close} cash={st.cash:.2f} positions[{pos}] "
                f"paused={r.paused} tripped: {trips}"
            )


def _set_paused(mode: str, sleeve: str, paused: bool) -> None:
    from live.state import SqlStateStore

    settings = Settings()
    names = SLEEVES if sleeve == "all" else (sleeve,)
    with Session(create_engine(settings.postgres_dsn)) as s:
        store = SqlStateStore(s)
        for name in names:
            try:
                store.set_paused(mode, name, paused)
            except KeyError as exc:
                typer.echo(f"{mode}/{name}: {exc}", err=True)
                continue
            verb = "paused" if paused else "resumed"
            typer.echo(f"{mode}/{name}: {verb} (takes effect next cycle)")


@admin_app.command("pause")
def pause(
    mode: str = typer.Option(..., "--mode"), sleeve: str = typer.Option(..., "--sleeve")
) -> None:
    """Stop making new decisions (positions are held, pending sim orders dropped)."""
    _set_paused(mode, sleeve, True)


@admin_app.command("resume")
def resume(
    mode: str = typer.Option(..., "--mode"), sleeve: str = typer.Option(..., "--sleeve")
) -> None:
    _set_paused(mode, sleeve, False)


@admin_app.command("reset-kill-switch")
def reset_kill_switch(
    mode: str = typer.Option(..., "--mode"),
    sleeve: str = typer.Option(..., "--sleeve"),
    name: str = typer.Option(
        "all", "--name", help=f"one of {', '.join(KILL_SWITCH_NAMES)}, or all"
    ),
    by: str = typer.Option("admin-cli", "--by", help="Who is resetting (audit trail)"),
) -> None:
    """Reset a tripped kill switch by hand (spec 6.3: they never reset themselves).
    For drawdown/daily-loss, consider *why* before resetting; see docs/runbook.md."""
    from live.state import SqlStateStore

    settings = Settings()
    names = KILL_SWITCH_NAMES if name == "all" else (name,)
    now = datetime.now(UTC)
    with Session(create_engine(settings.postgres_dsn)) as s:
        store = SqlStateStore(s)
        for n in names:
            if store.reset(mode, sleeve, n, by, now):
                typer.echo(f"reset {mode}/{sleeve}/{n}")
        if name != "all" and not store.kill_switches(mode, sleeve).get(name):
            typer.echo(f"{mode}/{sleeve}/{name} was not tripped")


@admin_app.command("rebaseline")
def rebaseline_cmd(
    mode: str = typer.Option(..., "--mode"), sleeve: str = typer.Option(..., "--sleeve")
) -> None:
    """Accept the venue's current holdings as the reconciliation baseline
    (after a Binance testnet wipe, or a deliberate manual trade). Then
    reset the reconcile_mismatch kill switch."""
    from live.build import build_runtime
    from live.sleeve_runner import rebaseline
    from live.state import SqlStateStore

    settings = Settings()
    with Session(create_engine(settings.postgres_dsn)) as s:
        rt = build_runtime(mode, sleeve, settings, SqlStateStore(s), s, RecordingAlerter(), "admin")
        new = rebaseline(rt, datetime.now(UTC))
    typer.echo(f"{mode}/{sleeve} baseline is now {new}")


@ibkr_app.command("contracts")
def ibkr_contracts(
    mode: str = typer.Option("sandbox", "--mode", help="sandbox (paper) or live"),
) -> None:
    """Qualify every equities listing against the running IB Gateway and
    store conId-backed tick size (minTick) and whole-share step in the
    instruments table. Refuses ambiguous matches."""
    from execution.brokers import ibkr

    settings = Settings()
    sleeve = load_sleeve("equities")
    port = settings.ibkr_paper_port if mode == "sandbox" else settings.ibkr_live_port
    ib = ibkr.connect(settings.ibkr_host, port, settings.ibkr_client_id + 9)
    try:
        with Session(create_engine(settings.postgres_dsn)) as s:
            for asset in sleeve.assets:
                listing = asset.listing_for(mode)
                from ib_async import Stock

                details = ib.reqContractDetails(
                    Stock(
                        listing.symbol,
                        "SMART",
                        listing.quote_currency,
                        primaryExchange=listing.exchange or "",
                    )
                )
                if len(details) != 1:
                    typer.echo(
                        f"  {listing.symbol}: {len(details)} matches -- NOT stored, check the listing",
                        err=True,
                    )
                    continue
                d = details[0]
                rec = s.get(
                    InstrumentRecord, (listing.venue.value, listing.symbol)
                ) or InstrumentRecord(
                    venue=listing.venue.value,
                    symbol=listing.symbol,
                    asset_class="equity",
                    base_currency=listing.base_currency,
                    quote_currency=listing.quote_currency,
                )
                rec.tick_size = Decimal(str(d.minTick))
                rec.step_size = Decimal(1)
                rec.min_notional = Decimal(0)
                s.merge(rec)
                typer.echo(
                    f"  {listing.symbol}: conId={d.contract.conId} {d.longName} "
                    f"exch={d.contract.primaryExchange} ccy={d.contract.currency} minTick={d.minTick}"
                )
            s.commit()
    finally:
        ib.disconnect()
