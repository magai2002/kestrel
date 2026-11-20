"""The per-cycle decision trace (live/trace.py) behind the dashboard's
Decisions page: it reports what plan_rebalance actually did, and the
public API serves it without leaking any absolute amount."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import live_helpers as h
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from api.app import create_public_app
from live.sleeve_runner import run_cycle
from live.state import InMemoryStateStore, SqlStateStore


def _decisions(store: InMemoryStateStore) -> list[dict[str, Any]]:
    return [e.payload for e in store.event_log if e.event_type == "decision"]


def test_trace_matches_the_decision_that_was_traded(tmp_path: Path) -> None:
    data = h.bars()
    rt = h.runtime(tmp_path, data)
    for i in range(80):
        run_cycle(rt, h.at(i), h.bar_close(i))
    assert isinstance(rt.store, InMemoryStateStore)
    traces = [d for d in _decisions(rt.store) if d["status"] == "decided"]
    assert traces, "every completed cycle records a decision trace"
    targets = [e.payload for e in rt.store.event_log if e.event_type == "target"]
    assert len(targets) == len(traces)
    for trace, target in zip(traces, targets, strict=True):
        [strat] = trace["strategies"]
        assert strat["strategy"] == "trend_vol_target"
        for aid, a in trace["assets"].items():
            # the trace reports plan_rebalance's own numbers, not a re-derivation
            assert a["raw"] == target["raw"][aid]
            assert a["constrained"] == target["weights"][aid]
            assert a["buffered"] == target["buffered"][aid]
            # strategy internals add up to the weight the strategy returned
            assert strat["weights"][aid] == target["raw"][aid]
            s = strat["assets"][aid]
            assert len(s["speeds"]) == len(h.PARAMS.speeds_days)
            if s["forecast_long_only"] is not None and s["vol_target_weight"] is not None:
                expected = min(
                    1.0,
                    s["forecast_long_only"] / 10 * s["vol_target_weight"] * s["instrument_weight"],
                )
                assert abs(expected - s["weight"]) < 1e-9
            assert a["outside_buffer"] == (abs(a["gap"]) > trace["buffer_pct"])
    acted = [a for t in traces for a in t["assets"].values() if a["order"]]
    assert acted and all(a["order"]["size_pct_of_equity"] > 0 for a in acted)


def test_halted_cycle_records_risk_headroom(tmp_path: Path) -> None:
    rt = h.runtime(tmp_path, h.bars())
    run_cycle(rt, h.at(0), h.bar_close(0))
    rt.store.trip("sim", "crypto", "drawdown", "test", h.at(0))
    run_cycle(rt, h.at(1), h.bar_close(1))
    assert isinstance(rt.store, InMemoryStateStore)
    last = _decisions(rt.store)[-1]
    assert last["status"] == "halted" and last["tripped"] == ["drawdown"]
    risk = last["risk"]
    assert risk["daily_pnl"]["limit"] == -rt.sleeve.risk.daily_loss_limit_pct
    assert risk["drawdown"]["limit"] == -rt.sleeve.risk.drawdown_limit_pct
    assert risk["data"]["age_seconds"] == 60


def test_public_decisions_and_metrics_endpoints_leak_nothing(tmp_path: Path, fresh_db: Any) -> None:
    factory = sessionmaker(fresh_db)
    with factory() as s:
        rt = h.runtime(tmp_path, h.bars(), store=SqlStateStore(s))  # type: ignore[arg-type]
        for i in range(70):
            run_cycle(rt, h.at(i), h.bar_close(i))
        state, _ = rt.store.load("sim", "crypto")
        assert state is not None
        secrets = {str(state.cash), *(str(q) for q in state.positions.values() if q)}
    client = TestClient(create_public_app(factory, clock=lambda: h.at(70) + timedelta(minutes=1)))
    dec = client.get("/api/decisions", params={"sleeve": "crypto"}).json()
    assert dec["latest_decided"]["status"] == "decided"
    assert set(dec["latest_decided"]["assets"]) == {"BTC", "ETH"}
    assert len(dec["series"]) == 70
    met = client.get("/api/metrics", params={"sleeve": "crypto"}).json()
    assert met["n_bars"] == 70 and met["sharpe"] is not None
    everything = json.dumps([dec, met])
    for secret in secrets:
        if Decimal(secret) != 0:
            assert secret not in everything, f"absolute value {secret} leaked"
    for word in ("cash", "quantity", "notional"):
        assert f'"{word}"' not in everything
