"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Iterator

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
