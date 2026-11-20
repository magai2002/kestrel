"""Phase 7 go-live gates (spec Section 13), enforced in code.

``live`` mode refuses to start unless **every** gate passes for the
sleeve. ``check_live_gates`` evaluates all of them and reports each one
(so ``kestrel live-check`` shows the whole checklist, not just the first
failure); ``require_live_unlocked`` raises ``LiveModeLocked`` with every
failing reason.

| # | Gate | How it's enforced |
| - | ---- | ----------------- |
| 1 | `KESTREL_LIVE_TRADING_ENABLED=true` **and** `i_understand_this_uses_real_money: true` in the sleeve config | both read here; neither defaults on |
| 2 | `max_live_capital` set; sizing capped to it whatever the venue balance | required here; the runner sizes from a ledger seeded with it (`live/build.py`) |
| 3 | ≥ `min_sim_weeks` (8) of `sim` history on this server, and the sim-vs-shadow tracking gap within tolerance | read from `equity_snapshots` and the latest `shadow:sim` run |
| 4 | Every strategy in the sleeve has a validation report whose hold-out **was evaluated, exactly once** (not contaminated) and whose DSR ≥ `min_dsr` | read from `validation_reports` |
| 5 | Live keys trade-only (no withdrawal) and IP-restricted | can't be read back from Kraken's or IBKR's APIs: `keys_trade_only_confirmed: true` plus the runbook check |
| 6 | Live kill-switch limits at least as tight as paper; daily loss limit also in absolute currency | `daily_loss_limit_abs` required; live uses the same sleeve risk block as paper, so never looser |
| 7 | The runbook's emergency-stop procedure has been rehearsed in `sandbox` | an `emergency_stop_rehearsal` event recorded by `kestrel admin record-rehearsal` *and* the config flag |

Gate 4 is stricter than the spec's wording ("not marked contaminated"):
a strategy whose hold-out was never looked at is trivially "not
contaminated" and has no out-of-sample evidence beyond walk-forward. A
real-money decision should rest on the one-shot hold-out too.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from config.settings import Settings
from config.sleeve import SleeveConfig, load_sleeve
from core.orm import BacktestRunRecord, EquitySnapshotRecord, EventLogRecord, ValidationReportRecord


class LiveModeLocked(RuntimeError):
    pass


@dataclass(frozen=True)
class Gate:
    number: int
    name: str
    passed: bool
    detail: str


def _gate1(settings: Settings, sleeve: SleeveConfig) -> Gate:
    env = settings.live_trading_enabled
    ack = sleeve.live_gates.i_understand_this_uses_real_money
    ok = env and ack
    return Gate(
        1,
        "explicit opt-in",
        ok,
        f"KESTREL_LIVE_TRADING_ENABLED={env}, i_understand_this_uses_real_money={ack}",
    )


def _gate2(sleeve: SleeveConfig) -> Gate:
    cap = sleeve.live_gates.max_live_capital
    ok = cap is not None and cap > 0
    return Gate(2, "max live capital", ok, f"max_live_capital={cap}")


def _gate3(session: Session, sleeve: SleeveConfig, now: datetime) -> Gate:
    first, last = session.execute(
        select(
            func.min(EquitySnapshotRecord.timestamp), func.max(EquitySnapshotRecord.timestamp)
        ).where(EquitySnapshotRecord.mode == "sim", EquitySnapshotRecord.sleeve == sleeve.name)
    ).one()
    weeks = 0.0 if first is None else (min(last, now) - first) / timedelta(weeks=1)
    shadow = session.scalars(
        select(BacktestRunRecord)
        .where(BacktestRunRecord.sleeve == sleeve.name, BacktestRunRecord.kind == "shadow:sim")
        .order_by(desc(BacktestRunRecord.created_at))
        .limit(1)
    ).first()
    gap = None if shadow is None else shadow.summary.get("gap_pct_annualised")
    tol = sleeve.live_gates.max_tracking_gap_pct
    enough = weeks >= sleeve.live_gates.min_sim_weeks
    tracking_ok = gap is not None and abs(gap) <= tol
    return Gate(
        3,
        "sim history + tracking",
        enough and tracking_ok,
        f"{weeks:.1f} of {sleeve.live_gates.min_sim_weeks:g} weeks of sim; "
        f"tracking gap {'n/a' if gap is None else f'{gap:.2f}'}%/yr (tolerance {tol:g})",
    )


def _gate4(session: Session, sleeve: SleeveConfig) -> Gate:
    problems = []
    for alloc in sleeve.strategies:
        rep = session.scalars(
            select(ValidationReportRecord)
            .where(
                ValidationReportRecord.strategy == alloc.name,
                ValidationReportRecord.sleeve == sleeve.name,
                ValidationReportRecord.holdout_evaluated.is_(True),
            )
            .order_by(desc(ValidationReportRecord.created_at))
            .limit(1)
        ).first()
        if rep is None:
            problems.append(f"{alloc.name}: hold-out never evaluated")
            continue
        if rep.holdout_contaminated:
            problems.append(f"{alloc.name}: hold-out CONTAMINATED")
        dsr = rep.dsr
        if dsr is None or dsr < sleeve.live_gates.min_dsr:
            problems.append(f"{alloc.name}: DSR {dsr} < {sleeve.live_gates.min_dsr}")
    return Gate(4, "validation", not problems, "; ".join(problems) or "all strategies pass")


def _gate5(sleeve: SleeveConfig) -> Gate:
    ok = sleeve.live_gates.keys_trade_only_confirmed
    return Gate(
        5,
        "trade-only, IP-restricted keys",
        ok,
        "keys_trade_only_confirmed=" + str(ok) + " (verify per docs/runbook.md: no withdrawal "
        "permission, IP whitelist = the engine box, ADR 0007)",
    )


def _gate6(sleeve: SleeveConfig) -> Gate:
    abs_limit = sleeve.live_gates.daily_loss_limit_abs
    ok = abs_limit is not None and abs_limit > 0
    return Gate(
        6,
        "live kill-switch limits",
        ok,
        f"daily_loss_limit_abs={abs_limit}; percent limits shared with paper "
        f"(daily {sleeve.risk.daily_loss_limit_pct:.1%}, drawdown {sleeve.risk.drawdown_limit_pct:.1%})",
    )


def _gate7(session: Session, sleeve: SleeveConfig) -> Gate:
    rehearsed = (
        session.scalar(
            select(func.count(EventLogRecord.id)).where(
                EventLogRecord.mode == "sandbox",
                EventLogRecord.sleeve == sleeve.name,
                EventLogRecord.event_type == "emergency_stop_rehearsal",
            )
        )
        or 0
    )
    flag = sleeve.live_gates.emergency_stop_rehearsed
    return Gate(
        7,
        "emergency stop rehearsed",
        bool(rehearsed) and flag,
        f"{rehearsed} recorded sandbox rehearsal(s); emergency_stop_rehearsed={flag}",
    )


def check_live_gates(
    settings: Settings,
    sleeve_name: str,
    session: Session | None,
    now: datetime,
    sleeve_config: SleeveConfig | None = None,
) -> list[Gate]:
    sleeve = sleeve_config or load_sleeve(sleeve_name)
    gates = [_gate1(settings, sleeve), _gate2(sleeve), _gate5(sleeve), _gate6(sleeve)]
    if session is None:
        gates += [
            Gate(n, name, False, "needs the database")
            for n, name in (
                (3, "sim history + tracking"),
                (4, "validation"),
                (7, "emergency stop rehearsed"),
            )
        ]
    else:
        gates += [_gate3(session, sleeve, now), _gate4(session, sleeve), _gate7(session, sleeve)]
    return sorted(gates, key=lambda g: g.number)


def require_live_unlocked(
    settings: Settings,
    sleeve: str,
    session: Session | None = None,
    now: datetime | None = None,
    **_: Any,
) -> None:
    from datetime import UTC

    gates = check_live_gates(settings, sleeve, session, now or datetime.now(UTC))
    failed = [g for g in gates if not g.passed]
    if failed:
        lines = "\n".join(f"  gate {g.number} ({g.name}): {g.detail}" for g in failed)
        raise LiveModeLocked(f"live mode for {sleeve!r} is locked; failing gates:\n{lines}")
