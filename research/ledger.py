"""Trial ledger and hold-out lock (spec Section 7, steps 1 and 3).

**Why a ledger at all.** Try 50 parameter combinations and the best one
will look good by luck alone. The Deflated Sharpe Ratio corrects for
that, but only if it knows how many things were tried — so every trial
is written down, including the ones that never make it into a report.
The trial count is the number of *distinct parameter sets* ever
evaluated for a (strategy, sleeve), across every run: re-running the
same grid doesn't inflate it, but widening the grid after seeing a
result does — which is exactly the behaviour it should punish.

**Hold-out lock.** The most recent 25% of history is evaluated at most
once per strategy and sleeve. Every look is recorded; the second one
prints a loud warning and marks that report (and every later one)
"hold-out contaminated" — once you've seen the hold-out and gone back
to change anything, it is no longer out-of-sample, and the report says
so instead of quietly pretending.

Two implementations of one small protocol: ``SqlLedger`` (Postgres,
used by the CLI) and ``InMemoryLedger`` (unit tests).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.orm import HoldoutAccessRecord, TrialRecord, ValidationReportRecord


@dataclass(frozen=True)
class Trial:
    run_id: str
    strategy: str
    sleeve: str
    kind: str  # grid | holdout | backtest
    params: dict[str, Any]
    params_hash: str
    config_hash: str
    data_hash: str
    git_sha: str | None
    period_start: datetime
    period_end: datetime
    n_bars: int
    sharpe_per_period: float | None
    sharpe_annual: float | None
    metrics: dict[str, Any] = field(default_factory=dict)


class Ledger(Protocol):
    def record_trials(self, trials: list[Trial]) -> None: ...

    def trial_sharpes(self, strategy: str, sleeve: str) -> dict[str, float]:
        """Latest per-period Sharpe per distinct params_hash (grid trials only)."""
        ...

    def record_holdout_access(
        self, strategy: str, sleeve: str, holdout_start: datetime, run_id: str, git_sha: str | None
    ) -> int:
        """Records an access; returns how many accesses existed *before* this one."""
        ...

    def holdout_access_count(self, strategy: str, sleeve: str, holdout_start: datetime) -> int: ...

    def save_report(
        self, run_id: str, strategy: str, sleeve: str, report: dict[str, Any]
    ) -> None: ...


def _finite(x: float | None) -> float | None:
    if x is None or x != x or x in (float("inf"), float("-inf")):
        return None
    return float(x)


class SqlLedger:
    def __init__(self, session: Session) -> None:
        self._session = session

    def record_trials(self, trials: list[Trial]) -> None:
        self._session.add_all(
            TrialRecord(
                run_id=t.run_id,
                strategy=t.strategy,
                sleeve=t.sleeve,
                kind=t.kind,
                params=t.params,
                params_hash=t.params_hash,
                config_hash=t.config_hash,
                data_hash=t.data_hash,
                git_sha=t.git_sha,
                period_start=t.period_start,
                period_end=t.period_end,
                n_bars=t.n_bars,
                sharpe_per_period=_finite(t.sharpe_per_period),
                sharpe_annual=_finite(t.sharpe_annual),
                metrics=t.metrics,
            )
            for t in trials
        )
        self._session.commit()

    def trial_sharpes(self, strategy: str, sleeve: str) -> dict[str, float]:
        latest = (
            select(TrialRecord.params_hash, func.max(TrialRecord.id).label("max_id"))
            .where(
                TrialRecord.strategy == strategy,
                TrialRecord.sleeve == sleeve,
                TrialRecord.kind == "grid",
            )
            .group_by(TrialRecord.params_hash)
            .subquery()
        )
        rows = self._session.execute(
            select(TrialRecord.params_hash, TrialRecord.sharpe_per_period).join(
                latest, TrialRecord.id == latest.c.max_id
            )
        ).all()
        return {h: float(s) for h, s in rows if s is not None}

    def holdout_access_count(self, strategy: str, sleeve: str, holdout_start: datetime) -> int:
        return int(
            self._session.scalar(
                select(func.count(HoldoutAccessRecord.id)).where(
                    HoldoutAccessRecord.strategy == strategy,
                    HoldoutAccessRecord.sleeve == sleeve,
                    HoldoutAccessRecord.holdout_start == holdout_start,
                )
            )
            or 0
        )

    def record_holdout_access(
        self, strategy: str, sleeve: str, holdout_start: datetime, run_id: str, git_sha: str | None
    ) -> int:
        prior = self.holdout_access_count(strategy, sleeve, holdout_start)
        self._session.add(
            HoldoutAccessRecord(
                strategy=strategy,
                sleeve=sleeve,
                holdout_start=holdout_start,
                run_id=run_id,
                git_sha=git_sha,
            )
        )
        self._session.commit()
        return prior

    def save_report(self, run_id: str, strategy: str, sleeve: str, report: dict[str, Any]) -> None:
        holdout = report.get("holdout") or {}
        self._session.add(
            ValidationReportRecord(
                run_id=run_id,
                strategy=strategy,
                sleeve=sleeve,
                holdout_evaluated=bool(holdout.get("evaluated")),
                holdout_contaminated=bool(holdout.get("contaminated")),
                dsr=_finite((report.get("headline") or {}).get("dsr")),
                report=report,
            )
        )
        self._session.commit()


class InMemoryLedger:
    def __init__(self) -> None:
        self.trials: list[Trial] = []
        self.accesses: list[tuple[str, str, datetime, str]] = []
        self.reports: dict[str, dict[str, Any]] = {}

    def record_trials(self, trials: list[Trial]) -> None:
        self.trials.extend(trials)

    def trial_sharpes(self, strategy: str, sleeve: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for t in self.trials:
            matches = t.strategy == strategy and t.sleeve == sleeve and t.kind == "grid"
            sr = t.sharpe_per_period
            if matches and sr is not None and sr == sr:  # sr == sr: not NaN
                out[t.params_hash] = sr
        return out

    def holdout_access_count(self, strategy: str, sleeve: str, holdout_start: datetime) -> int:
        return sum(1 for a in self.accesses if a[:3] == (strategy, sleeve, holdout_start))

    def record_holdout_access(
        self, strategy: str, sleeve: str, holdout_start: datetime, run_id: str, git_sha: str | None
    ) -> int:
        prior = self.holdout_access_count(strategy, sleeve, holdout_start)
        self.accesses.append((strategy, sleeve, holdout_start, run_id))
        return prior

    def save_report(self, run_id: str, strategy: str, sleeve: str, report: dict[str, Any]) -> None:
        self.reports[run_id] = report
