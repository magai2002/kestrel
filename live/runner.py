"""The scheduler: the only module besides ``core.clock.WallClock`` that
deals in real time (design rule 2).

For each sleeve: wait until ``next bar close + bar_close_delay``, run a
cycle expecting that bar; if the venue hasn't published it yet
(``waiting``), retry every ``retry_seconds`` until ``max_wait``, then
run without the expectation so the stale-data kill switch can judge.
On start-up every sleeve runs one catch-up cycle immediately, so a
restart mid-bar resumes (and recovers unfinished orders) right away
instead of at the next close.

A cycle that raises is logged, alerted (rate-limited) and counted; the
loop never dies on one bad cycle. If the database itself is gone, the
exception propagates and Docker's restart policy brings the process
back.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial

import structlog

from core.clock import Clock
from live.schedule import last_bar_close, next_bar_close
from live.sleeve_runner import CycleResult, SleeveRuntime, run_cycle

log = structlog.get_logger(__name__)


@dataclass
class _Slot:
    rt: SleeveRuntime
    bar_close: datetime
    due: datetime
    first_attempt: datetime | None = None


def _calendar(rt: SleeveRuntime) -> str | None:
    return rt.listing(rt.sleeve.asset_ids[0]).calendar


def _next_slot(rt: SleeveRuntime, after: datetime) -> _Slot:
    close = next_bar_close(after, rt.sleeve.decision_bar, _calendar(rt))
    return _Slot(rt, close, close + timedelta(seconds=rt.sleeve.bar_close_delay_seconds))


def run_once(rt: SleeveRuntime, now: datetime) -> CycleResult:
    expected = last_bar_close(
        now - timedelta(seconds=rt.sleeve.bar_close_delay_seconds),
        rt.sleeve.decision_bar,
        _calendar(rt),
    )
    return run_cycle(rt, now, expected)


def run_forever(
    runtimes: list[SleeveRuntime],
    clock: Clock,
    stop: threading.Event,
    retry_seconds: float = 60,
    max_wait: timedelta = timedelta(minutes=30),
    heartbeat: Callable[[datetime], None] | None = None,
) -> None:
    now = clock.now()
    slots: list[_Slot] = []
    for rt in runtimes:
        _safe_cycle(rt, partial(run_once, rt, now))
        slots.append(_next_slot(rt, now))
    while not stop.is_set():
        now = clock.now()
        if heartbeat:
            heartbeat(now)
        due = [s for s in slots if s.due <= now]
        if not due:
            wait = min((s.due - now).total_seconds() for s in slots)
            stop.wait(max(1.0, min(wait, 30.0)))
            continue
        for slot in due:
            slot.first_attempt = slot.first_attempt or now
            give_up = now - slot.first_attempt >= max_wait
            expected = None if give_up else slot.bar_close
            result = _safe_cycle(slot.rt, partial(run_cycle, slot.rt, now, expected))
            if result is not None and result.status == "waiting" and not give_up:
                slot.due = now + timedelta(seconds=retry_seconds)
                continue
            slots[slots.index(slot)] = _next_slot(slot.rt, max(now, slot.bar_close))


def _safe_cycle(rt: SleeveRuntime, fn: Callable[[], CycleResult]) -> CycleResult | None:
    try:
        return fn()
    except Exception as exc:  # a bad cycle must not take the loop down
        log.exception("cycle_failed", mode=rt.mode, sleeve=rt.name)
        rt.alert("cycle failed", f"{type(exc).__name__}: {exc}", "cycle_failed", "high")
        return None


def install_signal_handlers(stop: threading.Event) -> None:
    def handler(signum: int, _frame: object) -> None:
        log.info("shutdown_requested", signal=signum)
        stop.set()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
