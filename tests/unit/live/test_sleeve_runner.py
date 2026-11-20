"""Live runner behaviour: bar confirmation, idempotent re-runs, the
sandbox path against a venue, reconciliation and every kill switch."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import live_helpers as h
import pandas as pd

from execution.brokers.fake import FakeVenue
from live.sleeve_runner import SleeveRuntime, rebaseline, run_cycle
from live.state import InMemoryStateStore


def _venue(data: dict[str, pd.DataFrame], i: int) -> FakeVenue:
    v = FakeVenue(
        {s: Decimal(str(df["close"].iloc[i])) for s, df in data.items()},
        balances={"USDT": Decimal(10_000), "BTCUSDT": Decimal("0.5")},
    )
    return v


def _step(rt: SleeveRuntime, venue: FakeVenue | None, data: dict[str, pd.DataFrame], i: int):
    if venue is not None:
        venue.market_price = {s: Decimal(str(df["close"].iloc[i])) for s, df in data.items()}
        venue.now = h.at(i)
    return run_cycle(rt, h.at(i), h.bar_close(i))


def test_waits_when_expected_bar_not_published_yet(tmp_path: Path) -> None:
    data = h.bars()
    rt = h.runtime(tmp_path, data)
    # The feed only has bars up to i=5, but we expect bar 6's close; it's not stale yet.
    result = run_cycle(rt, h.at(5, delay=60), h.bar_close(6))
    assert result.status == "waiting"


def test_rerunning_the_same_bar_is_a_noop(tmp_path: Path) -> None:
    data = h.bars()
    rt = h.runtime(tmp_path, data)
    for i in range(40):
        run_cycle(rt, h.at(i), h.bar_close(i))
    snapshots = len(rt.store.snapshots)  # type: ignore[attr-defined]
    again = run_cycle(rt, h.at(39, delay=300), h.bar_close(39))
    assert again.status == "no_new_bar"
    assert len(rt.store.snapshots) == snapshots  # type: ignore[attr-defined]


def test_sandbox_trades_and_reconciles_against_venue_with_baseline(tmp_path: Path) -> None:
    data = h.bars()
    venue = _venue(data, 0)
    rt = h.runtime(tmp_path, data, mode="sandbox", broker=venue)
    for i in range(h.N):
        result = _step(rt, venue, data, i)
        assert result.status == "done", (i, result.detail, result.tripped)
    state, _ = rt.store.load("sandbox", "crypto")
    assert state is not None
    # The venue started with 0.5 BTC the sleeve doesn't own: baseline absorbs it.
    assert state.baseline["BTCUSDT"] == Decimal("0.5")
    assert venue.balances["BTCUSDT"] == Decimal("0.5") + state.positions["BTC"]
    assert venue.placed_count > 0
    assert rt.store.kill_switches("sandbox", "crypto") == {}


def test_manual_trade_on_venue_trips_reconcile_switch_until_reset(tmp_path: Path) -> None:
    data = h.bars()
    venue = _venue(data, 0)
    store = InMemoryStateStore()
    rt = h.runtime(tmp_path, data, mode="sandbox", broker=venue, store=store)
    for i in range(30):
        _step(rt, venue, data, i)
    venue.balances["ETHUSDT"] = venue.balances.get("ETHUSDT", Decimal(0)) + Decimal(
        "3"
    )  # someone traded by hand
    tripped = _step(rt, venue, data, 30)
    assert tripped.status == "halted" and "reconcile_mismatch" in tripped.tripped
    assert any("reconcile_mismatch" in a[2] for a in rt.alerter.sent)  # type: ignore[attr-defined]
    # Stays tripped on later bars even if nothing changes...
    assert _step(rt, venue, data, 31).status == "halted"
    # ...until a human rebaselines and resets.
    rebaseline(rt, h.at(31))
    assert store.reset("sandbox", "crypto", "reconcile_mismatch", "test", h.at(31))
    assert _step(rt, venue, data, 32).status == "done"


def test_drawdown_switch_trips_cancels_and_stays_tripped(tmp_path: Path) -> None:
    data = h.bars()
    crash = data["BTCUSDT"].copy()
    crash.loc[60:, ["open", "high", "low", "close"]] *= 0.3
    data["BTCUSDT"] = crash
    data["ETHUSDT"].loc[60:, ["open", "high", "low", "close"]] *= 0.3
    rt = h.runtime(tmp_path, data, drawdown_limit_pct=0.05)
    statuses = [run_cycle(rt, h.at(i), h.bar_close(i)).status for i in range(h.N)]
    first_halt = statuses.index("halted")
    assert all(s == "halted" for s in statuses[first_halt:])
    assert "drawdown" in rt.store.kill_switches("sim", "crypto")


def test_stale_feed_trips_stale_data_even_without_new_bars(tmp_path: Path) -> None:
    data = h.bars()
    rt = h.runtime(tmp_path, data)
    for i in range(30):
        run_cycle(rt, h.at(i), h.bar_close(i))
    rt.feed.fail = True  # type: ignore[attr-defined]
    # Three days later, still no bar newer than i=29: more than 2 bar intervals.
    later = h.at(29) + timedelta(days=3)
    result = run_cycle(rt, later, None)
    assert result.status == "halted" and result.tripped == ["stale_data"]
    assert rt.store.kill_switches("sim", "crypto")["stale_data"].tripped


def test_clock_drift_trips(tmp_path: Path) -> None:
    data = h.bars()
    venue = _venue(data, 0)
    rt = h.runtime(tmp_path, data, mode="sandbox", broker=venue)
    _step(rt, venue, data, 0)
    venue.market_price = {s: Decimal(str(df["close"].iloc[1])) for s, df in data.items()}
    venue.now = h.at(1) - timedelta(minutes=5)  # venue clock 5 min behind ours
    result = run_cycle(rt, h.at(1), h.bar_close(1))
    assert "clock_drift" in result.tripped


def test_paused_sleeve_marks_equity_but_does_not_trade(tmp_path: Path) -> None:
    data = h.bars()
    rt = h.runtime(tmp_path, data)
    run_cycle(rt, h.at(0), h.bar_close(0))
    rt.store.set_paused("sim", "crypto", True)
    for i in range(1, 80):
        assert run_cycle(rt, h.at(i), h.bar_close(i)).status == "paused"
    state, _ = rt.store.load("sim", "crypto")
    assert state is not None and all(q == 0 for q in state.positions.values())


def test_flatten_on_trip_sells_everything_once(tmp_path: Path) -> None:
    data = h.bars()
    for sym in data:
        data[sym].loc[60:, ["open", "high", "low", "close"]] *= 0.7
    venue = _venue(data, 0)
    rt = h.runtime(
        tmp_path, data, mode="sandbox", broker=venue, drawdown_limit_pct=0.05, flatten_on_trip=True
    )
    for i in range(59):
        _step(rt, venue, data, i)
    state, _ = rt.store.load("sandbox", "crypto")
    assert state is not None and any(q > 0 for q in state.positions.values()), (
        "needs a position to flatten"
    )
    results = [_step(rt, venue, data, i) for i in range(59, 70)]
    assert any("drawdown" in r.tripped for r in results)
    state, _ = rt.store.load("sandbox", "crypto")
    assert state is not None and all(q == 0 for q in state.positions.values())
    flatten_orders = [cid for cid in venue.orders if "-flatten-" in cid]
    assert 1 <= len(flatten_orders) <= 2  # one per held asset, sent on the tripping cycle only
