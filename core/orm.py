"""SQLAlchemy persistence models (spec Section 4.4: "Postgres 16 via
SQLAlchemy 2.0 and Alembic").

Phase 0 added ``instruments`` (static reference data). Phase 2 adds
``event_log`` — the backtest engine's "done when" literally requires
writing one (Section 12). ``orders``, ``fills``, ``positions_snapshots``,
``equity_snapshots``, ``kill_switch_state``, ``trial_ledger``,
``backtest_runs`` and ``validation_reports`` (the spec's remaining
minimum-table list) get added as the phases that need queryable,
non-JSON-blob access to them arrive — the event log is the full audit
trail on its own (design rule 7) until then.

This is deliberately a separate module from ``core/instrument.py``:
that one is the pydantic domain type used in business logic and
validation (frozen, no notion of "a row in a database"); this one is
the mutable, nullable-until-populated persistence shape. Keeping them
distinct means a migration changing how we store something never
forces a change to how strategies or risk checks reason about it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, DateTime, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class InstrumentRecord(Base):
    __tablename__ = "instruments"

    venue: Mapped[str] = mapped_column(String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    asset_class: Mapped[str] = mapped_column(String(16), nullable=False)
    base_currency: Mapped[str] = mapped_column(String(16), nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(16), nullable=False)
    # Nullable: exchange-metadata fetchers don't exist until the
    # execution layer (Phase 2) needs tick/step/min-notional for order
    # rounding (design rule 4).
    tick_size: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    step_size: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    min_notional: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EventLogRecord(Base):
    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sleeve: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
