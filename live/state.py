"""Durable state for the live runner (design rule 7's event log is the
audit trail; *this* is the working memory a restarted process resumes
from).

``SleeveState`` is the ledger for one (mode, sleeve): our own record of
cash and positions, built from fills. For ``sandbox``/``live`` the
venue is the source of truth and the reconciler compares the two every
cycle; for ``sim`` the ledger *is* the account.

``baseline`` exists because a venue account is rarely empty when a
sleeve starts: the Binance testnet hands every account a pile of test
BTC/ETH, an IBKR paper account may already hold positions. The
reconciler expects ``venue_qty == baseline + ledger_qty``. After a
testnet wipe that's false (correctly: the kill switch trips), and
``kestrel admin rebaseline`` accepts the venue's new state by hand.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from core.event_log import Event, write_events
from core.orm import (
    EquitySnapshotRecord,
    FillRecord,
    KillSwitchRecord,
    OrderRecord,
    PositionsSnapshotRecord,
    SleeveStateRecord,
)
from risk.kill_switch import KillSwitchState


@dataclass
class SleeveState:
    initial_capital: Decimal
    cash: Decimal
    positions: dict[str, Decimal]  # asset id -> quantity
    equity_peak: float
    day: str | None = None  # UTC date of day_open_equity
    day_open_equity: float = 0.0
    last_equity: float | None = None  # previous mark: the next UTC day's loss reference
    last_bar_close: str | None = None  # ISO; the last decision bar processed
    baseline: dict[str, Decimal] = field(default_factory=dict)  # venue symbol -> qty at start
    pending_orders: list[dict[str, Any]] = field(default_factory=list)  # sim: fill at next open
    error_times: list[str] = field(default_factory=list)  # ISO, recent broker errors
    started_at: str | None = None

    @classmethod
    def fresh(cls, capital: Decimal, asset_ids: list[str], started_at: datetime) -> SleeveState:
        return cls(
            initial_capital=capital,
            cash=capital,
            positions={a: Decimal(0) for a in asset_ids},
            equity_peak=float(capital),
            started_at=started_at.isoformat(),
        )

    def to_json(self) -> dict[str, Any]:
        def conv(x: Any) -> Any:
            if isinstance(x, Decimal):
                return str(x)
            if isinstance(x, dict):
                return {k: conv(v) for k, v in x.items()}
            if isinstance(x, list):
                return [conv(v) for v in x]
            return x

        return conv(asdict(self))  # type: ignore[no-any-return]

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> SleeveState:
        return cls(
            initial_capital=Decimal(raw["initial_capital"]),
            cash=Decimal(raw["cash"]),
            positions={k: Decimal(v) for k, v in raw["positions"].items()},
            equity_peak=float(raw["equity_peak"]),
            day=raw.get("day"),
            day_open_equity=float(raw.get("day_open_equity", 0.0)),
            last_equity=None if raw.get("last_equity") is None else float(raw["last_equity"]),
            last_bar_close=raw.get("last_bar_close"),
            baseline={k: Decimal(v) for k, v in (raw.get("baseline") or {}).items()},
            pending_orders=list(raw.get("pending_orders") or []),
            error_times=list(raw.get("error_times") or []),
            started_at=raw.get("started_at"),
        )


@dataclass(frozen=True)
class OrderRow:
    client_order_id: str
    sleeve: str
    asset_id: str
    venue: str
    symbol: str
    side: str
    quantity: Decimal
    limit_price: Decimal | None
    decision_price: Decimal | None
    status: str  # intent | open | filled | cancelled | rejected | error
    bar_close_ts: datetime
    broker_order_id: str | None = None
    filled_quantity: Decimal = Decimal(0)
    avg_fill_price: Decimal | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class FillRow:
    client_order_id: str
    venue_fill_id: str
    asset_id: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    timestamp: datetime
    decision_price: Decimal | None = None


class StateStore(Protocol):
    def load(self, mode: str, sleeve: str) -> tuple[SleeveState | None, bool]: ...
    def save(self, mode: str, sleeve: str, state: SleeveState, heartbeat: datetime) -> None: ...
    def set_paused(self, mode: str, sleeve: str, paused: bool) -> None: ...
    def kill_switches(self, mode: str, sleeve: str) -> dict[str, KillSwitchState]: ...
    def trip(self, mode: str, sleeve: str, name: str, reason: str, at: datetime) -> None: ...
    def reset(self, mode: str, sleeve: str, name: str, by: str, at: datetime) -> bool: ...
    def upsert_order(self, mode: str, row: OrderRow) -> None: ...
    def unfinished_orders(self, mode: str, sleeve: str) -> list[OrderRow]: ...
    def record_fill(self, mode: str, sleeve: str, fill: FillRow) -> bool: ...
    def snapshot(
        self,
        mode: str,
        sleeve: str,
        ts: datetime,
        equity: Decimal,
        cash: Decimal,
        equity_index: float,
        drawdown: float,
        gross: float,
        positions: dict[str, Any],
    ) -> None: ...
    def events(self, events: list[Event]) -> None: ...


_UNFINISHED = ("intent", "open", "error")


class SqlStateStore:
    def __init__(self, session: Session) -> None:
        self.s = session

    def load(self, mode: str, sleeve: str) -> tuple[SleeveState | None, bool]:
        rec = self.s.get(SleeveStateRecord, (mode, sleeve))
        if rec is None:
            return None, False
        return SleeveState.from_json(rec.state), rec.paused

    def save(self, mode: str, sleeve: str, state: SleeveState, heartbeat: datetime) -> None:
        stmt = insert(SleeveStateRecord).values(
            mode=mode, sleeve=sleeve, state=state.to_json(), paused=False, heartbeat_at=heartbeat
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["mode", "sleeve"],
            set_={"state": stmt.excluded.state, "heartbeat_at": stmt.excluded.heartbeat_at},
        )
        self.s.execute(stmt)
        self.s.commit()

    def set_paused(self, mode: str, sleeve: str, paused: bool) -> None:
        rec = self.s.get(SleeveStateRecord, (mode, sleeve))
        if rec is None:
            raise KeyError(f"no state for {mode}/{sleeve} yet (has the runner started once?)")
        rec.paused = paused
        self.s.commit()

    def kill_switches(self, mode: str, sleeve: str) -> dict[str, KillSwitchState]:
        rows = self.s.scalars(
            select(KillSwitchRecord).where(
                KillSwitchRecord.mode == mode, KillSwitchRecord.sleeve == sleeve
            )
        ).all()
        return {
            r.name: KillSwitchState(tripped=r.tripped, reason=r.reason, tripped_at=r.tripped_at)
            for r in rows
        }

    def trip(self, mode: str, sleeve: str, name: str, reason: str, at: datetime) -> None:
        stmt = insert(KillSwitchRecord).values(
            mode=mode, sleeve=sleeve, name=name, tripped=True, reason=reason[:500], tripped_at=at
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["mode", "sleeve", "name"],
            set_={
                "tripped": True,
                "reason": stmt.excluded.reason,
                "tripped_at": at,
                "reset_at": None,
                "reset_by": None,
            },
        )
        self.s.execute(stmt)
        self.s.commit()

    def reset(self, mode: str, sleeve: str, name: str, by: str, at: datetime) -> bool:
        rec = self.s.get(KillSwitchRecord, (mode, sleeve, name))
        if rec is None or not rec.tripped:
            return False
        rec.tripped = False
        rec.reset_at = at
        rec.reset_by = by
        self.s.commit()
        return True

    def upsert_order(self, mode: str, row: OrderRow) -> None:
        values = {**asdict(row), "mode": mode}
        stmt = insert(OrderRecord).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["client_order_id", "mode"],
            set_={
                k: stmt.excluded[k]
                for k in (
                    "status",
                    "broker_order_id",
                    "filled_quantity",
                    "avg_fill_price",
                    "last_error",
                    "limit_price",
                )
            },
        )
        self.s.execute(stmt)
        self.s.commit()

    def unfinished_orders(self, mode: str, sleeve: str) -> list[OrderRow]:
        rows = self.s.scalars(
            select(OrderRecord).where(
                OrderRecord.mode == mode,
                OrderRecord.sleeve == sleeve,
                OrderRecord.status.in_(_UNFINISHED),
            )
        ).all()
        return [
            OrderRow(
                client_order_id=r.client_order_id,
                sleeve=r.sleeve,
                asset_id=r.asset_id,
                venue=r.venue,
                symbol=r.symbol,
                side=r.side,
                quantity=r.quantity,
                limit_price=r.limit_price,
                decision_price=r.decision_price,
                status=r.status,
                bar_close_ts=r.bar_close_ts,
                broker_order_id=r.broker_order_id,
                filled_quantity=r.filled_quantity,
                avg_fill_price=r.avg_fill_price,
                last_error=r.last_error,
            )
            for r in rows
        ]

    def record_fill(self, mode: str, sleeve: str, fill: FillRow) -> bool:
        stmt = (
            insert(FillRecord)
            .values(mode=mode, sleeve=sleeve, **asdict(fill))
            .on_conflict_do_nothing(constraint="uq_fills_mode_venue_fill_id")
            .returning(FillRecord.id)
        )
        new = self.s.execute(stmt).first() is not None
        self.s.commit()
        return new

    def snapshot(
        self,
        mode: str,
        sleeve: str,
        ts: datetime,
        equity: Decimal,
        cash: Decimal,
        equity_index: float,
        drawdown: float,
        gross: float,
        positions: dict[str, Any],
    ) -> None:
        eq = (
            insert(EquitySnapshotRecord)
            .values(
                mode=mode,
                sleeve=sleeve,
                timestamp=ts,
                equity=equity,
                cash=cash,
                equity_index=equity_index,
                drawdown=drawdown,
                gross_exposure=gross,
            )
            .on_conflict_do_nothing(constraint="uq_equity_mode_sleeve_ts")
        )
        pos = (
            insert(PositionsSnapshotRecord)
            .values(mode=mode, sleeve=sleeve, timestamp=ts, positions=positions)
            .on_conflict_do_nothing(constraint="uq_positions_mode_sleeve_ts")
        )
        self.s.execute(eq)
        self.s.execute(pos)
        self.s.commit()

    def events(self, events: list[Event]) -> None:
        write_events(self.s, events)


class InMemoryStateStore:
    """Same behaviour, no database: unit tests and chaos scenarios."""

    def __init__(self) -> None:
        self.states: dict[tuple[str, str], tuple[dict[str, Any], bool]] = {}
        self.switches: dict[tuple[str, str, str], KillSwitchState] = {}
        self.orders: dict[tuple[str, str], OrderRow] = {}
        self.fills: dict[tuple[str, str], tuple[str, FillRow]] = {}
        self.snapshots: list[dict[str, Any]] = []
        self.event_log: list[Event] = []

    def load(self, mode: str, sleeve: str) -> tuple[SleeveState | None, bool]:
        raw = self.states.get((mode, sleeve))
        if raw is None:
            return None, False
        return SleeveState.from_json(raw[0]), raw[1]

    def save(self, mode: str, sleeve: str, state: SleeveState, heartbeat: datetime) -> None:
        paused = self.states.get((mode, sleeve), ({}, False))[1]
        self.states[(mode, sleeve)] = (state.to_json(), paused)

    def set_paused(self, mode: str, sleeve: str, paused: bool) -> None:
        raw, _ = self.states[(mode, sleeve)]
        self.states[(mode, sleeve)] = (raw, paused)

    def kill_switches(self, mode: str, sleeve: str) -> dict[str, KillSwitchState]:
        return {n: s for (m, sl, n), s in self.switches.items() if (m, sl) == (mode, sleeve)}

    def trip(self, mode: str, sleeve: str, name: str, reason: str, at: datetime) -> None:
        self.switches[(mode, sleeve, name)] = KillSwitchState(True, reason, at)

    def reset(self, mode: str, sleeve: str, name: str, by: str, at: datetime) -> bool:
        cur = self.switches.get((mode, sleeve, name))
        if cur is None or not cur.tripped:
            return False
        self.switches[(mode, sleeve, name)] = KillSwitchState()
        return True

    def upsert_order(self, mode: str, row: OrderRow) -> None:
        prev = self.orders.get((mode, row.client_order_id))
        if prev is not None:
            row = OrderRow(
                **{
                    **asdict(prev),
                    **{
                        k: getattr(row, k)
                        for k in (
                            "status",
                            "broker_order_id",
                            "filled_quantity",
                            "avg_fill_price",
                            "last_error",
                            "limit_price",
                        )
                    },
                }
            )
        self.orders[(mode, row.client_order_id)] = row

    def unfinished_orders(self, mode: str, sleeve: str) -> list[OrderRow]:
        return [
            r
            for (m, _), r in self.orders.items()
            if m == mode and r.sleeve == sleeve and r.status in _UNFINISHED
        ]

    def record_fill(self, mode: str, sleeve: str, fill: FillRow) -> bool:
        key = (mode, fill.venue_fill_id)
        if key in self.fills:
            return False
        self.fills[key] = (sleeve, fill)
        return True

    def snapshot(
        self,
        mode: str,
        sleeve: str,
        ts: datetime,
        equity: Decimal,
        cash: Decimal,
        equity_index: float,
        drawdown: float,
        gross: float,
        positions: dict[str, Any],
    ) -> None:
        self.snapshots.append(
            {
                "mode": mode,
                "sleeve": sleeve,
                "ts": ts,
                "equity": equity,
                "cash": cash,
                "equity_index": equity_index,
                "drawdown": drawdown,
                "gross": gross,
                "positions": positions,
            }
        )

    def events(self, events: list[Event]) -> None:
        self.event_log.extend(events)
