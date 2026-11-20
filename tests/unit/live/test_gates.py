"""Phase 7: live mode refuses to start unless every spec Section 13 gate
passes; each gate is checked independently."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import live_helpers as h
import pytest
from sqlalchemy.orm import Session

from config.settings import Settings
from config.sleeve import load_sleeve
from core.event_log import Event, EventType, write_events
from core.orm import BacktestRunRecord, EquitySnapshotRecord, ValidationReportRecord
from live.build import build_runtime
from live.gates import LiveModeLocked, check_live_gates, require_live_unlocked
from live.sleeve_runner import run_cycle
from live.state import InMemoryStateStore
from observability.alerts import RecordingAlerter

NOW = datetime(2027, 3, 1, tzinfo=UTC)


def _unlocked_sleeve() -> Any:
    sleeve = load_sleeve("crypto")
    gates = sleeve.live_gates.model_copy(
        update={
            "i_understand_this_uses_real_money": True,
            "max_live_capital": Decimal(2000),
            "daily_loss_limit_abs": Decimal(60),
            "keys_trade_only_confirmed": True,
            "emergency_stop_rehearsed": True,
        }
    )
    return sleeve.model_copy(update={"live_gates": gates})


def _seed_evidence(
    s: Session,
    weeks: float = 9,
    gap: float = 0.5,
    dsr: float = 0.97,
    contaminated: bool = False,
    rehearsed: bool = True,
) -> None:
    start = NOW - timedelta(weeks=weeks)
    for i in range(3):
        s.add(
            EquitySnapshotRecord(
                mode="sim",
                sleeve="crypto",
                timestamp=start + (NOW - start) * i / 2,
                equity=Decimal(10000),
                cash=Decimal(0),
                equity_index=100.0,
                drawdown=0.0,
                gross_exposure=0.0,
            )
        )
    s.add(
        BacktestRunRecord(
            run_id="shadow-1",
            strategy="trend_vol_target",
            sleeve="crypto",
            kind="shadow:sim",
            git_sha=None,
            config_hash="x",
            data_hash="y",
            summary={"gap_pct_annualised": gap},
        )
    )
    s.add(
        ValidationReportRecord(
            run_id="v-1",
            strategy="trend_vol_target",
            sleeve="crypto",
            holdout_evaluated=True,
            holdout_contaminated=contaminated,
            dsr=dsr,
            report={},
        )
    )
    s.commit()
    if rehearsed:
        write_events(
            s,
            [Event("admin", "crypto", EventType.EMERGENCY_STOP_REHEARSAL, NOW, {}, mode="sandbox")],
        )


def _settings(enabled: bool = True) -> Settings:
    return Settings(_env_file=None, live_trading_enabled=enabled)  # type: ignore[call-arg]


def test_default_configuration_is_locked_with_every_reason() -> None:
    with pytest.raises(LiveModeLocked) as info:
        require_live_unlocked(_settings(enabled=False), "crypto", None, NOW)
    msg = str(info.value)
    for n in range(1, 8):
        assert f"gate {n} " in msg


def test_build_runtime_refuses_live_before_touching_any_broker(tmp_path: Path) -> None:
    with pytest.raises(LiveModeLocked):
        build_runtime(
            "live",
            "crypto",
            _settings(enabled=False),
            InMemoryStateStore(),
            None,
            RecordingAlerter(),
            "t",
        )


def test_all_gates_pass_with_full_evidence(fresh_db: Any) -> None:
    with Session(fresh_db) as s:
        _seed_evidence(s)
        gates = check_live_gates(_settings(), "crypto", s, NOW, _unlocked_sleeve())
    assert [g.number for g in gates if not g.passed] == []


@pytest.mark.parametrize(
    ("evidence", "settings_enabled", "failing"),
    [
        ({}, False, 1),  # env flag off
        ({"weeks": 3}, True, 3),  # not enough sim history
        ({"gap": 9.0}, True, 3),  # sim and shadow backtest disagree
        ({"contaminated": True}, True, 4),
        ({"dsr": 0.80}, True, 4),
        ({"rehearsed": False}, True, 7),
    ],
)
def test_each_gate_fails_independently(
    fresh_db: Any, evidence: dict[str, Any], settings_enabled: bool, failing: int
) -> None:
    with Session(fresh_db) as s:
        _seed_evidence(s, **evidence)
        gates = check_live_gates(_settings(settings_enabled), "crypto", s, NOW, _unlocked_sleeve())
    assert [g.number for g in gates if not g.passed] == [failing]


def test_live_ledger_seeded_with_capital_cap_and_absolute_daily_loss(tmp_path: Path) -> None:
    data = h.bars()
    for sym in data:
        data[sym].loc[45:, ["open", "high", "low", "close"]] *= 0.9
    rt = h.runtime(tmp_path, data)
    rt.capital_cap = Decimal(5000)
    rt.daily_loss_limit_abs = Decimal(50)
    run_cycle(rt, h.at(0), h.bar_close(0))
    state, _ = rt.store.load("sim", "crypto")
    assert (
        state is not None and state.initial_capital == Decimal(5000) and state.cash == Decimal(5000)
    )
    results = [run_cycle(rt, h.at(i), h.bar_close(i)) for i in range(1, 60)]
    tripped = [r for r in results if "daily_loss" in r.tripped]
    assert tripped, "a 10% gap on a position should breach a 50 USD absolute daily limit"
    assert "absolute limit" in str(rt.store.kill_switches("sim", "crypto")["daily_loss"].reason)
