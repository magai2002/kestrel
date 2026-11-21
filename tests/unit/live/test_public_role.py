"""The public API works with nothing more than the SELECT-only role
(api/db_role.py), and that role can't write or read anything else.

Lives next to live_helpers (bare-module import, see CLAUDE.md) because it
needs real cycles to put rows in every table the API reads."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import live_helpers as h
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from api.app import create_public_app
from api.db_role import PUBLIC_ROLE, ensure_public_role
from api.queries import PUBLIC_TABLES
from core.orm import Base
from live.sleeve_runner import run_cycle
from live.state import SqlStateStore


def _as_public_role(owner_engine: Any) -> Any:
    engine = create_engine(owner_engine.url)

    @event.listens_for(engine, "connect")
    def _set_role(dbapi_conn: Any, _: Any) -> None:
        with dbapi_conn.cursor() as cur:
            cur.execute(f"SET ROLE {PUBLIC_ROLE}")
        dbapi_conn.commit()

    return engine


@pytest.fixture
def public_engine(fresh_db: Any) -> Any:
    """The full schema (empty), reached as the public role."""
    with fresh_db.begin() as conn:
        ensure_public_role(conn, None)
    engine = _as_public_role(fresh_db)
    yield engine
    engine.dispose()


def test_public_tables_exist_and_exclude_the_sensitive_ones() -> None:
    assert set(PUBLIC_TABLES) <= set(Base.metadata.tables)
    for private in ("orders", "instruments", "trial_ledger", "holdout_access"):
        assert private not in PUBLIC_TABLES


def test_every_public_endpoint_works_as_the_public_role(
    tmp_path: Path, fresh_db: Any, public_engine: Any
) -> None:
    with sessionmaker(fresh_db)() as s:  # real rows in every table the API reads
        rt = h.runtime(tmp_path, h.bars(), store=SqlStateStore(s))  # type: ignore[arg-type]
        for i in range(70):
            run_cycle(rt, h.at(i), h.bar_close(i))
    app = create_public_app(sessionmaker(public_engine), clock=lambda: h.at(70) + timedelta(1))
    client = TestClient(app)  # a permission error would raise here, not return 500
    params = {"sleeve": "crypto"}
    checked = []
    for route in app.openapi()["paths"]:  # every route, even with docs off
        if route == "/api/stream":  # never ends; same queries as /api/status
            continue
        path = route.replace("{strategy_id}", "trend_vol_target")
        resp = client.get(path, params=params)
        assert resp.status_code in (200, 404), (path, resp.text)  # 404 = no report stored
        checked.append(path)
    assert {"/api/status", "/api/decisions", "/api/metrics", "/api/trades"} <= set(checked)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders",
        "SELECT * FROM instruments",
        "SELECT * FROM holdout_access",
        "INSERT INTO kill_switch_state (mode, sleeve, name, tripped) VALUES ('sim','x','y',false)",
        "UPDATE sleeve_state SET paused = true",
        "DELETE FROM event_log",
        "CREATE TABLE pwned (x int)",
    ],
)
def test_public_role_cannot(public_engine: Any, sql: str) -> None:
    with public_engine.connect() as conn, pytest.raises(DBAPIError, match="permission denied"):
        conn.execute(text(sql))
