"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from config.settings import Settings


@pytest.fixture
def pg_session() -> Iterator[Session]:
    """A session on the real local/CI Postgres (migrated to head), or skip.
    Tests using it must scope their rows by a unique run id/strategy
    name -- the database is shared, not reset between tests."""
    engine = create_engine(Settings().postgres_dsn)
    try:
        with engine.connect() as conn:
            conn.execute(select(1))
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Postgres not reachable: {exc}")
    with Session(engine) as session:
        yield session


@pytest.fixture
def fresh_db() -> Iterator[Any]:
    """A throwaway database (created and dropped per test) on the local
    Postgres, with the full schema. For tests that need predictable rows
    under the real sleeve names without touching the dev database."""
    from uuid import uuid4

    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    from core.orm import Base

    base_url = make_url(Settings().postgres_dsn)
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(select(1))
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Postgres not reachable: {exc}")
    name = f"kestrel_test_{uuid4().hex[:10]}"
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(base_url.set(database=name))
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()
