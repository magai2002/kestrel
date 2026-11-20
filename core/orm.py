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

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Numeric, String, func
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


class TrialRecord(Base):
    """One row per backtest trial — every grid point, every walk-forward
    window's chosen config, every hold-out evaluation (spec Section 7,
    step 3). The DSR's trial count and Sharpe variance come from here."""

    __tablename__ = "trial_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sleeve: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # grid | holdout | backtest
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    params_hash: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    config_hash: Mapped[str] = mapped_column(String(32), nullable=False)
    data_hash: Mapped[str] = mapped_column(String(32), nullable=False)
    git_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    n_bars: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sharpe_per_period: Mapped[float | None] = mapped_column(Float, nullable=True)
    sharpe_annual: Mapped[float | None] = mapped_column(Float, nullable=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class HoldoutAccessRecord(Base):
    """Every time anyone looks at a sleeve's hold-out for a strategy.
    A second row for the same (strategy, sleeve, holdout_start) marks
    every later report "hold-out contaminated" (spec Section 7, step 1)."""

    __tablename__ = "holdout_access"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sleeve: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    holdout_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    git_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    accessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ValidationReportRecord(Base):
    """The full JSON report, so the API (Phase 6) and the live gates
    (Phase 7) read the same thing the HTML was rendered from."""

    __tablename__ = "validation_reports"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sleeve: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    holdout_evaluated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    holdout_contaminated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    dsr: Mapped[float | None] = mapped_column(Float, nullable=True)
    report: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class BacktestRunRecord(Base):
    """One row per event-engine run (``kestrel backtest``, shadow backtests)."""

    __tablename__ = "backtest_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sleeve: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # backtest | shadow
    git_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config_hash: Mapped[str] = mapped_column(String(32), nullable=False)
    data_hash: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
