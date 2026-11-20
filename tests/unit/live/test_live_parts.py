"""Unit tests for the smaller live pieces: schedule, reconcile, kill-switch
checks, alert rate limiting, store upserts, client ids, SQL state store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pandas as pd
import pytest
from sqlalchemy.orm import Session

from core.enums import Timeframe
from data import store
from execution.brokers.client_ids import attempt_client_id, venue_client_id
from live.reconcile import reconcile
from live.schedule import last_bar_close, next_bar_close, sessions_between
from live.state import FillRow, OrderRow, SleeveState, SqlStateStore
from observability.alerts import RateLimiter, RecordingAlerter
from risk import kill_switch as ks


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)


# --- schedule ---------------------------------------------------------------
def test_crypto_4h_grid() -> None:
    assert next_bar_close(utc(2026, 11, 20, 13, 7), Timeframe.H4, None) == utc(2026, 11, 20, 16)
    assert next_bar_close(utc(2026, 11, 20, 16), Timeframe.H4, None) == utc(2026, 11, 20, 20)
    assert last_bar_close(utc(2026, 11, 20, 13, 7), Timeframe.H4, None) == utc(2026, 11, 20, 12)


def test_lse_closes_including_christmas_eve_early_close_and_weekend() -> None:
    assert next_bar_close(utc(2026, 12, 23, 17), Timeframe.D1, "LSE") == utc(2026, 12, 24, 12, 30)
    # Friday after the close -> next close is Monday's.
    assert next_bar_close(utc(2026, 11, 20, 17), Timeframe.D1, "LSE") == utc(2026, 11, 23, 16, 30)
    assert last_bar_close(utc(2026, 11, 22, 12), Timeframe.D1, "LSE") == utc(2026, 11, 20, 16, 30)


def test_weekend_is_not_stale_for_exchange_sleeve() -> None:
    friday_close = utc(2026, 11, 20, 16, 30)
    assert sessions_between(friday_close, utc(2026, 11, 23, 9), "LSE") == 0
    assert sessions_between(friday_close, utc(2026, 11, 24, 17), "LSE") == 2


# --- reconcile --------------------------------------------------------------
def test_reconcile_ok_with_baseline_and_within_one_step() -> None:
    res = reconcile(
        {"BTC": Decimal("0.1")},
        {"BTC": "BTCUSDT"},
        {"BTCUSDT": Decimal("1")},
        {"BTCUSDT": Decimal("1.10001")},
        {"BTC": Decimal("0.00001")},
        0.0,
    )
    assert res.ok


def test_reconcile_flags_position_and_cash_mismatch() -> None:
    res = reconcile(
        {"BTC": Decimal("0.1")},
        {"BTC": "BTCUSDT"},
        {},
        {"BTCUSDT": Decimal("0")},
        {"BTC": Decimal("0.00001")},
        0.01,
        ledger_cash=Decimal(5000),
        venue_cash=Decimal(100),
    )
    assert set(res.mismatches) == {"BTCUSDT", "cash"}


# --- kill switch checks -----------------------------------------------------
def test_live_kill_switch_checks() -> None:
    now = utc(2026, 1, 1, 12)
    assert ks.check_stale_data(now - timedelta(hours=9), now, 4 * 3600, 2)
    assert ks.check_stale_data(now - timedelta(hours=7), now, 4 * 3600, 2) is None
    assert ks.check_stale_data(None, now, 3600, 2)
    errs = [now - timedelta(minutes=m) for m in (1, 2, 3, 4, 20)]
    assert ks.check_error_burst(errs, now, 4, 15)
    assert ks.check_error_burst(errs, now, 5, 15) is None
    assert ks.check_clock_drift(now + timedelta(seconds=6), now, 5)
    assert ks.check_clock_drift(now + timedelta(seconds=4), now, 5) is None
    assert ks.check_reconcile({}) is None


# --- alerts -----------------------------------------------------------------
def test_rate_limiter_suppresses_flapping_alerts() -> None:
    t = [0.0]
    limiter = RateLimiter(60, clock=lambda: t[0])
    assert limiter.allow("k")
    assert not limiter.allow("k")
    assert limiter.allow("other")
    t[0] = 61
    assert limiter.allow("k")


def test_recording_alerter_respects_rate_limit() -> None:
    a = RecordingAlerter(min_interval_seconds=3600)
    assert a.send("t", "m", "k") and not a.send("t", "m", "k")
    assert len(a.sent) == 1


# --- client ids -------------------------------------------------------------
def test_client_ids_are_deterministic_and_venue_safe() -> None:
    cid = "equities-portfolio-20261120T163000Z-VUSD-0"
    assert venue_client_id(cid) == venue_client_id(cid)
    assert venue_client_id(cid) != venue_client_id(attempt_client_id(cid, 1))
    assert attempt_client_id(cid, 2) == "equities-portfolio-20261120T163000Z-VUSD-2"
    assert len(venue_client_id(cid)) == 36


# --- store upsert -----------------------------------------------------------
def _bar(t: str, close: float) -> dict[str, object]:
    ot = pd.Timestamp(t, tz="UTC")
    return {
        "venue": "kraken",
        "symbol": "BTC-USD",
        "timeframe": "4h",
        "open_time": ot,
        "close_time": ot + pd.Timedelta(hours=4),
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1.0,
    }


def test_upsert_bars_appends_and_overwrites_without_losing_history(tmp_path: Path) -> None:
    store.upsert_bars(
        pd.DataFrame([_bar("2026-01-01 00:00", 1), _bar("2026-01-01 04:00", 2)]), tmp_path
    )
    store.upsert_bars(
        pd.DataFrame([_bar("2026-01-01 04:00", 2.5), _bar("2026-01-01 08:00", 3)]), tmp_path
    )
    df = store.read_bars(tmp_path, "kraken", "BTC-USD", "4h")
    assert list(df["close"]) == [1, 2.5, 3]


# --- SQL state store (real Postgres) -----------------------------------------
def test_sql_state_store_round_trip(pg_session: Session) -> None:
    s = SqlStateStore(pg_session)
    sleeve = f"t{uuid4().hex[:8]}"
    now = utc(2026, 1, 1, 12)
    st = SleeveState.fresh(Decimal(10000), ["BTC"], now)
    st.baseline = {"BTCUSDT": Decimal("0.5")}
    s.save("sim", sleeve, st, now)
    loaded, paused = s.load("sim", sleeve)
    assert loaded == st and paused is False
    s.set_paused("sim", sleeve, True)
    assert s.load("sim", sleeve)[1] is True
    s.save("sim", sleeve, st, now)  # saving state must not un-pause
    assert s.load("sim", sleeve)[1] is True

    s.trip("sim", sleeve, "drawdown", "dd", now)
    assert s.kill_switches("sim", sleeve)["drawdown"].tripped
    assert s.reset("sim", sleeve, "drawdown", "test", now)
    assert not s.kill_switches("sim", sleeve)["drawdown"].tripped

    row = OrderRow(
        f"{sleeve}-x-0",
        sleeve,
        "BTC",
        "fake",
        "BTCUSDT",
        "buy",
        Decimal(1),
        Decimal(100),
        Decimal(100),
        "intent",
        now,
    )
    s.upsert_order("sim", row)
    assert [r.client_order_id for r in s.unfinished_orders("sim", sleeve)] == [row.client_order_id]
    s.upsert_order(
        "sim", OrderRow(**{**row.__dict__, "status": "filled", "filled_quantity": Decimal(1)})
    )
    assert s.unfinished_orders("sim", sleeve) == []

    fill = FillRow(
        row.client_order_id,
        f"{sleeve}-f1",
        "BTC",
        "buy",
        Decimal(1),
        Decimal(100),
        Decimal("0.1"),
        now,
    )
    assert s.record_fill("sim", sleeve, fill) is True
    assert s.record_fill("sim", sleeve, fill) is False  # deduplicated on venue fill id

    s.snapshot("sim", sleeve, now, Decimal(10000), Decimal(10000), 100.0, 0.0, 0.0, {})
    s.snapshot(
        "sim", sleeve, now, Decimal(10000), Decimal(10000), 100.0, 0.0, 0.0, {}
    )  # idempotent


def test_sql_store_rejects_pause_before_first_run(pg_session: Session) -> None:
    with pytest.raises(KeyError):
        SqlStateStore(pg_session).set_paused("sim", f"none{uuid4().hex[:6]}", True)
