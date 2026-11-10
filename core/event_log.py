"""The append-only event log (design rule 7): every bar, signal/target,
risk decision, order, fill and kill-switch event becomes one row, JSON
payload and all. It's the audit trail, and from Phase 4 on, the input
for replaying a live day through the backtester — which means the
payload shape for a given ``EventType`` needs to stay stable once
something actually depends on replaying it.

Lives in ``core/`` rather than ``live/`` (where the spec's repo layout
puts an ``event_log.py``) because the backtest engine needs to write
events too (Phase 2's "done when" literally says so), and that need
exists before ``live/`` does. When Phase 4 builds the live runner,
``live/event_log.py`` is expected to be a thin wrapper around
``write_events`` below, not a parallel implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from core.orm import EventLogRecord


class EventType(StrEnum):
    BAR = "bar"
    TARGET = "target"  # a strategy's target weights for one bar
    RISK_DECISION = "risk_decision"
    ORDER = "order"
    FILL = "fill"
    KILL_SWITCH = "kill_switch"
    # RECONCILIATION arrives in Phase 4, once there's a broker to reconcile against.


@dataclass(frozen=True)
class Event:
    run_id: str
    sleeve: str
    event_type: EventType
    timestamp: datetime
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("Event.timestamp must be timezone-aware")


def write_events(session: Session, events: list[Event]) -> None:
    """Bulk-insert events. Append-only: never updates or deletes a row."""
    if not events:
        return
    session.add_all(
        EventLogRecord(
            run_id=event.run_id,
            sleeve=event.sleeve,
            event_type=event.event_type.value,
            timestamp=event.timestamp,
            payload=event.payload,
        )
        for event in events
    )
    session.commit()
