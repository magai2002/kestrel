"""FastAPI apps (spec Section 8.1).

Two **separate apps on separate ports**, not one app with an "admin"
prefix (ADR 0006):

- ``create_public_app`` — read-only, rate-limited, shareable numbers
  only; serves the built dashboard (``web/dist``) at ``/`` and the API
  at ``/api``. This is the only thing host Nginx proxies.
- ``create_admin_app`` — pause/resume, reset kill switches, full status.
  Bound to ``127.0.0.1`` on its own port and never proxied: reachable
  over SSH or WireGuard only, plus the ``kestrel admin`` CLI that does
  the same things straight against the database.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from api import queries
from api.ratelimit import RateLimitMiddleware, TokenBucket
from risk.kill_switch import KILL_SWITCH_NAMES

SessionFactory = Callable[[], Session]
SLEEVES = ("crypto", "equities")


def _session_dep(factory: SessionFactory) -> Callable[[], Iterator[Session]]:
    def dep() -> Iterator[Session]:
        s = factory()
        try:
            yield s
        finally:
            s.close()

    return dep


def _check(sleeve: str | None = None, mode: str | None = None) -> None:
    if sleeve is not None and sleeve not in SLEEVES:
        raise HTTPException(404, f"unknown sleeve {sleeve!r}")
    if mode is not None and mode not in queries.MODES:
        raise HTTPException(404, f"unknown mode {mode!r}")


@lru_cache(maxsize=8)
def _sleeve(name: str) -> Any:
    from config.sleeve import load_sleeve

    return load_sleeve(name)


def _periods_per_year(sleeve: str) -> float:
    return float(_sleeve(sleeve).periods_per_year)


def _next_decision(sleeve: str, mode: str, now: datetime) -> str | None:
    """When the runner will next act for this sleeve: the next bar close
    plus the sleeve's publication delay, on the runner's own schedule
    (live/runner.py::_slot). Display only; never raises."""
    from live.schedule import next_bar_close

    try:
        cfg = _sleeve(sleeve)
        delay = timedelta(seconds=cfg.bar_close_delay_seconds)
        calendar = cfg.asset(cfg.asset_ids[0]).listing_for(mode).calendar
        return (next_bar_close(now - delay, cfg.decision_bar, calendar) + delay).isoformat()
    except Exception:
        return None


def create_public_app(
    session_factory: SessionFactory,
    web_dist: Path | None = None,
    rate_per_second: float = 2.0,
    burst: int = 40,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    docs: bool = False,
    max_streams: int = 50,
) -> FastAPI:
    """``docs`` serves Swagger at ``/api/docs`` (off in production: it maps
    the attack surface for free). ``max_streams`` caps concurrent
    ``/api/stream`` connections across all clients; each holds a coroutine
    and a database query every few seconds."""
    app = FastAPI(
        title="kestrel public API",
        docs_url="/api/docs" if docs else None,
        openapi_url="/api/openapi.json" if docs else None,
        redoc_url=None,
    )
    app.add_middleware(RateLimitMiddleware, bucket=TokenBucket(rate_per_second, burst))
    db = _session_dep(session_factory)
    r = APIRouter(prefix="/api")

    @r.get("/status")
    def status(s: Session = Depends(db)) -> dict[str, Any]:
        now = clock()
        sleeves = queries.status(s, now)
        for row in sleeves:
            row["next_decision"] = _next_decision(row["sleeve"], row["mode"], now)
        return {
            "as_of": now.isoformat(),
            "sleeves": sleeves,
            "disclaimer": "Paper trading. Not financial advice.",
        }

    @r.get("/equity")
    def equity(sleeve: str, mode: str = "sim", s: Session = Depends(db)) -> dict[str, Any]:
        _check(sleeve, mode)
        return queries.equity(s, sleeve, mode)

    @r.get("/positions")
    def positions(mode: str = "sim", s: Session = Depends(db)) -> list[dict[str, Any]]:
        _check(mode=mode)
        return queries.positions(s, mode)

    @r.get("/trades")
    def trades(
        mode: str = "sim",
        sleeve: str | None = None,
        limit: int = Query(50, ge=1, le=500),
        s: Session = Depends(db),
    ) -> list[dict[str, Any]]:
        _check(sleeve, mode)
        return queries.trades(s, mode, sleeve, limit)

    @r.get("/strategies")
    def strategies(s: Session = Depends(db)) -> list[dict[str, Any]]:
        return queries.strategies(s)

    @r.get("/strategies/{strategy_id}/report")
    def report(strategy_id: str, sleeve: str, s: Session = Depends(db)) -> dict[str, Any]:
        _check(sleeve)
        rep = queries.strategy_report(s, strategy_id, sleeve)
        if rep is None:
            raise HTTPException(404, f"no validation report for {strategy_id}/{sleeve}")
        return rep

    @r.get("/tracking")
    def tracking(sleeve: str, mode: str = "sim", s: Session = Depends(db)) -> dict[str, Any]:
        _check(sleeve, mode)
        return queries.tracking(s, sleeve, mode)

    @r.get("/decisions")
    def decisions(
        sleeve: str,
        mode: str = "sim",
        history: int = Query(180, ge=1, le=1000),
        s: Session = Depends(db),
    ) -> dict[str, Any]:
        """Latest decision trace: strategy internals -> constraints ->
        buffer -> orders -> risk, plus kill-switch headroom."""
        _check(sleeve, mode)
        return queries.decisions(s, sleeve, mode, history)

    @r.get("/metrics")
    def metrics(sleeve: str, mode: str = "sim", s: Session = Depends(db)) -> dict[str, Any]:
        _check(sleeve, mode)
        return queries.metrics(s, sleeve, mode, _periods_per_year(sleeve))

    open_streams = [0]

    @r.get("/stream")
    async def stream(interval: float = Query(15.0, ge=5.0, le=120.0)) -> StreamingResponse:
        """Server-sent events: ``status`` every ``interval`` s, plus a
        ``fill`` event whenever new fills appear."""
        if open_streams[0] >= max_streams:
            raise HTTPException(503, "too many live connections; the page still polls")

        async def events() -> AsyncIterator[str]:
            # Counted from the first iteration, so a client that drops before
            # the body starts can't leak a slot (an unstarted generator never
            # runs its finally).
            open_streams[0] += 1
            try:
                async for chunk in _events():
                    yield chunk
            finally:
                open_streams[0] -= 1

        async def _events() -> AsyncIterator[str]:
            last_fill = -1
            while True:
                s = session_factory()
                try:
                    payload = {"sleeves": queries.status(s, clock()), "as_of": clock().isoformat()}
                    yield f"event: status\ndata: {json.dumps(payload)}\n\n"
                    newest = queries.recent_fill_id(s)
                    if last_fill >= 0 and newest > last_fill:
                        for t in queries.trades(s, "sim", None, min(50, newest - last_fill)):
                            yield f"event: fill\ndata: {json.dumps(t)}\n\n"
                    last_fill = newest
                finally:
                    s.close()
                await asyncio.sleep(interval)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @r.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"ok": "true"}

    app.include_router(r)

    if web_dist is not None and (web_dist / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            if path == "api" or path.startswith("api/"):
                raise HTTPException(404, "not found")  # unknown API path, not the dashboard
            candidate = (web_dist / path).resolve()
            if path and candidate.is_file() and web_dist.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(web_dist / "index.html")  # client-side routes

    return app


def create_admin_app(session_factory: SessionFactory, token: str | None = None) -> FastAPI:
    from live.state import SqlStateStore

    app = FastAPI(title="kestrel admin API (127.0.0.1 only)")
    db = _session_dep(session_factory)

    def auth(authorization: str | None = Header(default=None)) -> None:
        # Defence in depth: the port is loopback-only and never proxied,
        # but if a token is configured it is required as well.
        if token and authorization != f"Bearer {token}":
            raise HTTPException(401, "bad or missing admin token")

    r = APIRouter(prefix="/admin", dependencies=[Depends(auth)])

    @r.get("/status")
    def full_status(s: Session = Depends(db)) -> list[dict[str, Any]]:
        from core.orm import SleeveStateRecord

        return [
            {
                "mode": x.mode,
                "sleeve": x.sleeve,
                "paused": x.paused,
                "state": x.state,
                "heartbeat": x.heartbeat_at.isoformat() if x.heartbeat_at else None,
            }
            for x in s.query(SleeveStateRecord).all()
        ]

    @r.post("/{mode}/{sleeve}/pause")
    def pause(mode: str, sleeve: str, s: Session = Depends(db)) -> dict[str, str]:
        _check(sleeve, mode)
        SqlStateStore(s).set_paused(mode, sleeve, True)
        return {"status": "paused", "effective": "next cycle"}

    @r.post("/{mode}/{sleeve}/resume")
    def resume(mode: str, sleeve: str, s: Session = Depends(db)) -> dict[str, str]:
        _check(sleeve, mode)
        SqlStateStore(s).set_paused(mode, sleeve, False)
        return {"status": "resumed", "effective": "next cycle"}

    @r.post("/{mode}/{sleeve}/kill-switches/{name}/reset")
    def reset(
        mode: str, sleeve: str, name: str, by: str = "admin-api", s: Session = Depends(db)
    ) -> dict[str, Any]:
        _check(sleeve, mode)
        if name not in KILL_SWITCH_NAMES:
            raise HTTPException(404, f"unknown kill switch {name!r}")
        done = SqlStateStore(s).reset(mode, sleeve, name, by, datetime.now(UTC))
        return {"reset": done}

    @r.post("/{mode}/{sleeve}/reconcile")
    def reconcile_now(mode: str, sleeve: str) -> dict[str, str]:
        # Reconciliation needs the broker connection the engine process
        # holds; the runner reconciles at the start of every cycle. This
        # endpoint exists so the boundary is explicit, and says so.
        _check(sleeve, mode)
        return {
            "status": "reconciliation runs at the start of every cycle; "
            "use `kestrel admin status` to see the latest result"
        }

    app.include_router(r)
    return app
