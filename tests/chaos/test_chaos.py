"""Spec Section 11, "Chaos": kill the engine between order send and ack;
drop the network; inject 5xx and timeouts; restart mid-bar. **Each must
end reconciled with the correct position and no duplicate orders.**

Automated here against ``FakeVenue``'s fault injection, with a *fresh*
``SleeveRuntime`` standing in for a restarted process (only the state
store and the venue survive, exactly like a real restart). The manual,
real-venue versions of these drills (``docker kill`` mid-order, cutting
the container's network) are in ``tests/chaos/README.md``.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "unit" / "live"))
import live_helpers as h

from execution.brokers.fake import FakeVenue, Faults
from live.sleeve_runner import SleeveRuntime, run_cycle
from live.state import InMemoryStateStore


def _venue(data: dict[str, pd.DataFrame]) -> FakeVenue:
    return FakeVenue(
        {s: Decimal(str(df["close"].iloc[0])) for s, df in data.items()},
        balances={"USDT": Decimal(10_000)},
    )


def _restart(
    tmp: Path, data: dict[str, pd.DataFrame], venue: FakeVenue, store: InMemoryStateStore
) -> SleeveRuntime:
    """A brand-new process: new runtime, new strategy object; same DB, same venue."""
    return h.runtime(tmp, data, mode="sandbox", broker=venue, store=store)


def _step(rt: SleeveRuntime, venue: FakeVenue, data: dict[str, pd.DataFrame], i: int):
    venue.market_price = {s: Decimal(str(df["close"].iloc[i])) for s, df in data.items()}
    venue.now = h.at(i)
    return run_cycle(rt, h.at(i), h.bar_close(i))


def _assert_reconciled(store: InMemoryStateStore, venue: FakeVenue) -> None:
    state, _ = store.load("sandbox", "crypto")
    assert state is not None
    for aid, sym in (("BTC", "BTCUSDT"), ("ETH", "ETHUSDT")):
        assert (
            venue.balances.get(sym, Decimal(0))
            == state.baseline.get(sym, Decimal(0)) + state.positions[aid]
        )
    assert "reconcile_mismatch" not in store.kill_switches("sandbox", "crypto")
    # No client id was ever sent twice (FakeVenue rejects duplicates loudly,
    # and every placed order is a distinct id).
    assert venue.placed_count == len(venue.orders)


def _first_trading_bar(tmp: Path, data: dict[str, pd.DataFrame]) -> int:
    """Find the first bar on which the strategy sends an order (dry run)."""
    venue, store = _venue(data), InMemoryStateStore()
    rt = h.runtime(tmp / "probe", data, mode="sandbox", broker=venue, store=store)
    for i in range(h.N):
        before = venue.placed_count
        _step(rt, venue, data, i)
        if venue.placed_count > before:
            return i
    raise AssertionError("strategy never traded")


@pytest.fixture
def data() -> dict[str, pd.DataFrame]:
    return h.bars()


def test_engine_killed_between_send_and_ack(tmp_path: Path, data: dict[str, pd.DataFrame]) -> None:
    k = _first_trading_bar(tmp_path, data)
    venue, store = _venue(data), InMemoryStateStore()
    rt = _restart(tmp_path, data, venue, store)
    for i in range(k):
        _step(rt, venue, data, i)
    venue.faults = Faults(crash_after_place=1)
    with pytest.raises(SystemExit):
        _step(rt, venue, data, k)  # order reached the venue, process died before booking it
    assert any(r.status == "intent" for r in store.orders.values())
    placed_before_restart = venue.placed_count

    rt2 = _restart(tmp_path, data, venue, store)
    result = _step(rt2, venue, data, k)  # restart mid-bar: same bar again
    assert result.status == "done"
    # The order already at the venue was found and booked, not re-sent.
    assert venue.placed_count >= placed_before_restart
    sent_ids = list(venue.orders)
    assert len(sent_ids) == len(set(sent_ids))
    for i in range(k + 1, k + 15):
        _step(rt2, venue, data, i)
    _assert_reconciled(store, venue)


def test_5xx_after_venue_accepted_the_order(tmp_path: Path, data: dict[str, pd.DataFrame]) -> None:
    k = _first_trading_bar(tmp_path, data)
    venue, store = _venue(data), InMemoryStateStore()
    rt = _restart(tmp_path, data, venue, store)
    for i in range(k):
        _step(rt, venue, data, i)
    venue.faults = Faults(place_lost_ack=3)
    for i in range(k, k + 15):
        _step(rt, venue, data, i)
    _assert_reconciled(store, venue)


def test_network_drop_and_timeouts(tmp_path: Path, data: dict[str, pd.DataFrame]) -> None:
    k = _first_trading_bar(tmp_path, data)
    venue, store = _venue(data), InMemoryStateStore()
    rt = _restart(tmp_path, data, venue, store)
    for i in range(k):
        _step(rt, venue, data, i)
    venue.faults = Faults(place_unreachable=2, get_transient=2)
    for i in range(k, k + 15):
        _step(rt, venue, data, i)
    _assert_reconciled(store, venue)


def test_restart_mid_bar_after_a_completed_cycle(
    tmp_path: Path, data: dict[str, pd.DataFrame]
) -> None:
    k = _first_trading_bar(tmp_path, data)
    venue, store = _venue(data), InMemoryStateStore()
    rt = _restart(tmp_path, data, venue, store)
    for i in range(k + 1):
        _step(rt, venue, data, i)
    placed = venue.placed_count
    rt2 = _restart(tmp_path, data, venue, store)
    assert _step(rt2, venue, data, k).status == "no_new_bar"
    assert venue.placed_count == placed
    _assert_reconciled(store, venue)


def test_feed_outage_then_recovery(tmp_path: Path, data: dict[str, pd.DataFrame]) -> None:
    venue, store = _venue(data), InMemoryStateStore()
    rt = _restart(tmp_path, data, venue, store)
    for i in range(20):
        _step(rt, venue, data, i)
    rt.feed.fail = True  # type: ignore[attr-defined]
    assert _step(rt, venue, data, 20).status in ("waiting", "no_new_bar")
    rt.feed.fail = False  # type: ignore[attr-defined]
    assert _step(rt, venue, data, 21).status == "done"
    _assert_reconciled(store, venue)
