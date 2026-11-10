"""Exercises write_events against a real Postgres (the local
docker-compose instance / CI's service container) rather than mocking
SQLAlchemy -- the thing worth verifying is that the JSONB round-trips
and the row shape matches core.orm.EventLogRecord, which a mock can't
actually confirm.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from config.settings import Settings
from core.event_log import Event, EventType, write_events
from core.orm import EventLogRecord


@pytest.fixture
def db_session():
    engine = create_engine(Settings().postgres_dsn)
    try:
        with engine.connect() as conn:
            conn.execute(select(1))
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Postgres not reachable: {exc}")
    with Session(engine) as session:
        yield session


def test_write_events_round_trips_payload(db_session: Session) -> None:
    run_id = str(uuid4())
    events = [
        Event(
            run_id=run_id,
            sleeve="crypto",
            event_type=EventType.BAR,
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            payload={"symbol": "BTCUSDT", "close": 50000.0},
        ),
        Event(
            run_id=run_id,
            sleeve="crypto",
            event_type=EventType.TARGET,
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            payload={"weights": {"binance:BTCUSDT": 0.5}},
        ),
    ]
    write_events(db_session, events)

    rows = (
        db_session.execute(
            select(EventLogRecord)
            .where(EventLogRecord.run_id == run_id)
            .order_by(EventLogRecord.id)
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert rows[0].event_type == "bar"
    assert rows[0].payload == {"symbol": "BTCUSDT", "close": 50000.0}
    assert rows[1].event_type == "target"
    assert rows[1].payload["weights"]["binance:BTCUSDT"] == 0.5


def test_write_events_empty_list_is_a_no_op(db_session: Session) -> None:
    write_events(db_session, [])  # must not raise


def test_event_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="aware"):
        Event(
            run_id="x",
            sleeve="crypto",
            event_type=EventType.BAR,
            timestamp=datetime(2024, 1, 1),
            payload={},
        )
