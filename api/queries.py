"""Read-only queries behind the public API. Every function here returns
**shareable** numbers only (spec Section 8.1): equity indexed to 100,
weights in percent, prices, slippage in bps. Never cash, quantities,
notionals, fees in currency, or account ids — so the dashboard stays
safe to share even once real money is involved.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from core.orm import (
    BacktestRunRecord,
    EquitySnapshotRecord,
    EventLogRecord,
    FillRecord,
    KillSwitchRecord,
    PositionsSnapshotRecord,
    SleeveStateRecord,
    ValidationReportRecord,
)
from research import stats
from research.tables import clean

MODES = ("sim", "sandbox", "live")

# Every table this module reads, and the only ones the public API's
# Postgres role may SELECT (api/db_role.py). Reading a new table here
# means adding its model to this tuple; tests/unit/api/test_public_role.py
# runs every endpoint as that role and fails otherwise.
PUBLIC_TABLES: tuple[str, ...] = tuple(
    str(m.__tablename__)
    for m in (
        BacktestRunRecord,
        EquitySnapshotRecord,
        EventLogRecord,
        FillRecord,
        KillSwitchRecord,
        PositionsSnapshotRecord,
        SleeveStateRecord,
        ValidationReportRecord,
    )
)


def status(s: Session, now: datetime) -> list[dict[str, Any]]:
    out = []
    for rec in s.scalars(
        select(SleeveStateRecord).order_by(SleeveStateRecord.mode, SleeveStateRecord.sleeve)
    ):
        last_bar = rec.state.get("last_bar_close")
        switches = s.scalars(
            select(KillSwitchRecord).where(
                KillSwitchRecord.mode == rec.mode, KillSwitchRecord.sleeve == rec.sleeve
            )
        ).all()
        last_rec = s.scalars(
            select(EventLogRecord)
            .where(
                EventLogRecord.mode == rec.mode,
                EventLogRecord.sleeve == rec.sleeve,
                EventLogRecord.event_type == "reconciliation",
            )
            .order_by(desc(EventLogRecord.id))
            .limit(1)
        ).first()
        out.append(
            {
                "mode": rec.mode,
                "sleeve": rec.sleeve,
                "heartbeat": rec.heartbeat_at.isoformat() if rec.heartbeat_at else None,
                "last_bar_close": last_bar,
                "data_age_seconds": (now - datetime.fromisoformat(last_bar)).total_seconds()
                if last_bar
                else None,
                "paused": rec.paused,
                "kill_switches": {
                    k.name: {
                        "tripped": k.tripped,
                        "since": k.tripped_at.isoformat() if k.tripped_at else None,
                    }
                    for k in switches
                },
                "last_reconciliation": None
                if last_rec is None
                else {
                    "at": last_rec.timestamp.isoformat(),
                    "ok": bool(last_rec.payload.get("ok", True)),
                },
            }
        )
    return out


def equity(s: Session, sleeve: str, mode: str) -> dict[str, Any]:
    rows = s.execute(
        select(
            EquitySnapshotRecord.timestamp,
            EquitySnapshotRecord.equity_index,
            EquitySnapshotRecord.drawdown,
        )
        .where(EquitySnapshotRecord.sleeve == sleeve, EquitySnapshotRecord.mode == mode)
        .order_by(EquitySnapshotRecord.timestamp)
    ).all()
    return {
        "sleeve": sleeve,
        "mode": mode,
        "equity_index": [[t.isoformat(), float(e)] for t, e, _ in rows],
        "drawdown_pct": [[t.isoformat(), float(d) * 100] for t, _, d in rows],
    }


def positions(s: Session, mode: str) -> list[dict[str, Any]]:
    latest = (
        select(
            PositionsSnapshotRecord.sleeve, func.max(PositionsSnapshotRecord.timestamp).label("ts")
        )
        .where(PositionsSnapshotRecord.mode == mode)
        .group_by(PositionsSnapshotRecord.sleeve)
        .subquery()
    )
    rows = s.scalars(
        select(PositionsSnapshotRecord)
        .join(
            latest,
            (PositionsSnapshotRecord.sleeve == latest.c.sleeve)
            & (PositionsSnapshotRecord.timestamp == latest.c.ts),
        )
        .where(PositionsSnapshotRecord.mode == mode)
    ).all()
    return [
        {
            "sleeve": r.sleeve,
            "mode": mode,
            "as_of": r.timestamp.isoformat(),
            "weights_pct": {a: round(float(p["weight"]) * 100, 2) for a, p in r.positions.items()},
            "prices": {a: float(p["price"]) for a, p in r.positions.items()},
        }
        for r in rows
    ]


def trades(s: Session, mode: str, sleeve: str | None, limit: int) -> list[dict[str, Any]]:
    q = select(FillRecord).where(FillRecord.mode == mode)
    if sleeve:
        q = q.where(FillRecord.sleeve == sleeve)
    out = []
    for f in s.scalars(q.order_by(desc(FillRecord.timestamp)).limit(limit)):
        sign = 1 if f.side == "buy" else -1
        slip = None
        if f.decision_price:
            slip = float(sign * (f.price - f.decision_price) / f.decision_price * Decimal(10_000))
        notional = f.price * f.quantity
        fee_bps = float(f.fee / notional * Decimal(10_000)) if notional else None
        out.append(
            {
                "time": f.timestamp.isoformat(),
                "sleeve": f.sleeve,
                "asset": f.asset_id,
                "side": f.side,
                "decision_price": float(f.decision_price) if f.decision_price else None,
                "fill_price": float(f.price),
                "slippage_bps": slip,
                "fee_bps": fee_bps,
                # implementation shortfall per unit: adverse price move + fees, in bps
                "shortfall_bps": (slip or 0.0) + (fee_bps or 0.0) if slip is not None else None,
            }
        )
    return out


def strategies(s: Session) -> list[dict[str, Any]]:
    latest = (
        select(
            ValidationReportRecord.strategy,
            ValidationReportRecord.sleeve,
            func.max(ValidationReportRecord.created_at).label("created"),
        )
        .group_by(ValidationReportRecord.strategy, ValidationReportRecord.sleeve)
        .subquery()
    )
    rows = s.scalars(
        select(ValidationReportRecord)
        .join(
            latest,
            (ValidationReportRecord.strategy == latest.c.strategy)
            & (ValidationReportRecord.sleeve == latest.c.sleeve)
            & (ValidationReportRecord.created_at == latest.c.created),
        )
        .order_by(ValidationReportRecord.sleeve, ValidationReportRecord.strategy)
    ).all()
    return [
        {
            "id": r.strategy,
            "sleeve": r.sleeve,
            "run_id": r.run_id,
            "generated_at": r.created_at.isoformat(),
            "headline": r.report.get("headline"),
            "verdict": r.report.get("verdict"),
            "oos": {
                k: (r.report.get("oos") or {}).get(k)
                for k in ("cagr", "sharpe", "max_drawdown", "turnover_annual", "cost_drag_annual")
            },
            "holdout": {
                k: (r.report.get("holdout") or {}).get(k) for k in ("evaluated", "contaminated")
            },
        }
        for r in rows
    ]


def strategy_report(s: Session, strategy: str, sleeve: str) -> dict[str, Any] | None:
    r = s.scalars(
        select(ValidationReportRecord)
        .where(ValidationReportRecord.strategy == strategy, ValidationReportRecord.sleeve == sleeve)
        .order_by(desc(ValidationReportRecord.created_at))
        .limit(1)
    ).first()
    return None if r is None else r.report


def tracking(s: Session, sleeve: str, mode: str) -> dict[str, Any]:
    shadow = s.scalars(
        select(BacktestRunRecord)
        .where(BacktestRunRecord.sleeve == sleeve, BacktestRunRecord.kind == f"shadow:{mode}")
        .order_by(desc(BacktestRunRecord.created_at))
        .limit(1)
    ).first()
    shortfall = [
        t["shortfall_bps"] for t in trades(s, mode, sleeve, 500) if t["shortfall_bps"] is not None
    ]
    return {
        "sleeve": sleeve,
        "mode": mode,
        "shadow": clean(shadow.summary) if shadow else {"status": "not_run_yet"},
        "implementation_shortfall_bps": {
            "n_fills": len(shortfall),
            "mean": sum(shortfall) / len(shortfall) if shortfall else None,
            "max": max(shortfall) if shortfall else None,
        },
    }


def _clean(x: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], clean(x))


def _decision_events(s: Session, sleeve: str, mode: str, limit: int) -> list[EventLogRecord]:
    return list(
        s.scalars(
            select(EventLogRecord)
            .where(
                EventLogRecord.mode == mode,
                EventLogRecord.sleeve == sleeve,
                EventLogRecord.event_type == "decision",
            )
            .order_by(desc(EventLogRecord.timestamp), desc(EventLogRecord.id))
            .limit(limit)
        )
    )


def decisions(s: Session, sleeve: str, mode: str, history: int = 180) -> dict[str, Any]:
    """The latest decision trace (``live/trace.py``: shareable by
    construction) and, for charting, each recent decision's target vs
    held weight per asset."""
    rows = _decision_events(s, sleeve, mode, history)
    latest_decided = next((r for r in rows if r.payload.get("status") == "decided"), None)
    series = []
    for r in reversed(rows):
        assets = r.payload.get("assets") or {}
        series.append(
            {
                "time": r.timestamp.isoformat(),
                "status": r.payload.get("status"),
                "target": {a: v.get("constrained") for a, v in assets.items()},
                "held": {a: v.get("current") for a, v in assets.items()},
                "aim": {a: v.get("buffered") for a, v in assets.items()},
            }
        )
    return {
        "sleeve": sleeve,
        "mode": mode,
        "latest": None
        if not rows
        else {"time": rows[0].timestamp.isoformat(), **_clean(rows[0].payload)},
        "latest_decided": None
        if latest_decided is None
        else {"time": latest_decided.timestamp.isoformat(), **_clean(latest_decided.payload)},
        "series": clean(series),
    }


def metrics(s: Session, sleeve: str, mode: str, periods_per_year: float) -> dict[str, Any]:
    """Realised performance of the running sleeve, from its equity index
    (shareable: returns don't depend on the capital) and its weights."""
    import pandas as pd

    rows = s.execute(
        select(EquitySnapshotRecord.timestamp, EquitySnapshotRecord.equity_index)
        .where(EquitySnapshotRecord.sleeve == sleeve, EquitySnapshotRecord.mode == mode)
        .order_by(EquitySnapshotRecord.timestamp)
    ).all()
    out: dict[str, Any] = {
        "sleeve": sleeve,
        "mode": mode,
        "periods_per_year": periods_per_year,
        "n_bars": len(rows),
    }
    if len(rows) < 2:
        return out
    curve = pd.Series([float(e) for _, e in rows], index=[t for t, _ in rows])
    returns = curve.pct_change().dropna()
    snaps = s.scalars(
        select(PositionsSnapshotRecord)
        .where(PositionsSnapshotRecord.sleeve == sleeve, PositionsSnapshotRecord.mode == mode)
        .order_by(PositionsSnapshotRecord.timestamp)
    ).all()
    weights = pd.DataFrame(
        [{a: float(p["weight"]) for a, p in r.positions.items()} for r in snaps]
    ).fillna(0.0)
    dd = stats.max_drawdown(curve)
    days = (rows[-1][0] - rows[0][0]).total_seconds() / 86400
    n_fills = s.scalar(
        select(func.count(FillRecord.id)).where(
            FillRecord.sleeve == sleeve, FillRecord.mode == mode
        )
    )
    out.update(
        {
            "since": rows[0][0].isoformat(),
            "days": days,
            "total_return": stats.total_return(curve),
            # Annualising a few weeks of returns is mostly noise; the
            # dashboard says so, but the numbers are the honest formula.
            "cagr": stats.cagr(curve, periods_per_year) if days >= 30 else None,
            "vol_annual": stats.annualized_vol(returns, periods_per_year),
            "sharpe": stats.sharpe_ratio(returns, periods_per_year),
            "sortino": stats.sortino_ratio(returns, periods_per_year),
            "max_drawdown": dd.max_drawdown,
            "current_drawdown": float(curve.iloc[-1] / curve.cummax().iloc[-1] - 1.0),
            "exposure_avg": stats.exposure(weights),
            "turnover_annual": stats.turnover(weights) * periods_per_year,
            "best_bar": float(returns.max()),
            "worst_bar": float(returns.min()),
            "n_fills": int(n_fills or 0),
        }
    )
    return _clean(out)


def recent_fill_id(s: Session) -> int:
    return int(s.scalar(select(func.max(FillRecord.id))) or 0)


def utcnow() -> datetime:
    return datetime.now(UTC)
