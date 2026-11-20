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

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Numeric, String, UniqueConstraint, func
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
    # backtest | sim | sandbox | live (Phase 4). Nullable: Phase 2 rows predate it.
    mode: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
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


# --- Phase 4: live trading state ------------------------------------------
# Every table is keyed by (mode, sleeve): `sim` and `sandbox` run side by
# side against the same database (spec Section 3.2) and must never see
# each other's state.


class OrderRecord(Base):
    """One row per order *attempt* (each re-quote has its own client id).
    Written with status ``intent`` *before* the network call, so a crash
    mid-send leaves evidence to recover from (``live/recovery.py``)."""

    __tablename__ = "orders"

    client_order_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), primary_key=True)
    sleeve: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    asset_id: Mapped[str] = mapped_column(String(32), nullable=False)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    decision_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    filled_quantity: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False, default=0)
    avg_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    bar_close_ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class FillRecord(Base):
    """Append-only. ``(mode, venue_fill_id)`` is unique, so re-reading an
    order's fills after a restart can never double-count one."""

    __tablename__ = "fills"
    __table_args__ = (
        UniqueConstraint("mode", "venue_fill_id", name="uq_fills_mode_venue_fill_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    sleeve: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    client_order_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    venue_fill_id: Mapped[str] = mapped_column(String(128), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    fee: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    decision_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SleeveStateRecord(Base):
    """The runner's durable working state for one (mode, sleeve): ledger
    cash and positions, peak/day-open equity, broker baseline, pending
    sim orders, admin pause flag. Rewritten at the end of every cycle."""

    __tablename__ = "sleeve_state"

    mode: Mapped[str] = mapped_column(String(16), primary_key=True)
    sleeve: Mapped[str] = mapped_column(String(32), primary_key=True)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class EquitySnapshotRecord(Base):
    __tablename__ = "equity_snapshots"
    __table_args__ = (
        UniqueConstraint("mode", "sleeve", "timestamp", name="uq_equity_mode_sleeve_ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    sleeve: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    equity: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    cash: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    equity_index: Mapped[float] = mapped_column(Float, nullable=False)  # 100 at start
    drawdown: Mapped[float] = mapped_column(Float, nullable=False)
    gross_exposure: Mapped[float] = mapped_column(Float, nullable=False)


class PositionsSnapshotRecord(Base):
    __tablename__ = "positions_snapshots"
    __table_args__ = (
        UniqueConstraint("mode", "sleeve", "timestamp", name="uq_positions_mode_sleeve_ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    sleeve: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    positions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False
    )  # asset -> qty/price/weight


class KillSwitchRecord(Base):
    __tablename__ = "kill_switch_state"

    mode: Mapped[str] = mapped_column(String(16), primary_key=True)
    sleeve: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    tripped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    tripped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reset_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
