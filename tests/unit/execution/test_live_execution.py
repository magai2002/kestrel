"""The live order manager path: marketable limits, timeout, cancel and
re-quote, and idempotency under lost acks and unreachable venues."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from core.order import Order, OrderSide
from execution.brokers.base import BrokerOrderStatus
from execution.brokers.fake import FakeVenue, Faults
from execution.live_execution import (
    ExecutionPolicy,
    ListJournal,
    execute_marketable_limit,
    protected_limit,
)

T0 = datetime(2026, 1, 1, 12, tzinfo=UTC)
POLICY = ExecutionPolicy(
    protection_bps=Decimal("20"), timeout_seconds=10, poll_seconds=1, max_requotes=2
)


class FakeTime:
    def __init__(self) -> None:
        self.t = 0.0

    def sleep(self, s: float) -> None:
        self.t += s

    def elapsed(self) -> float:
        return self.t


def _order(qty: str = "0.1", side: OrderSide = OrderSide.BUY) -> Order:
    return Order("crypto-portfolio-20260101T120000Z-BTCUSDT-0", "BTC", side, Decimal(qty), T0)


def _run(venue: FakeVenue, order: Order, **kw: object):
    ft = FakeTime()
    journal = ListJournal()
    report = execute_marketable_limit(
        venue,
        order,
        "BTCUSDT",
        Decimal("50000"),
        Decimal("0.01"),
        POLICY,
        journal,
        ft.sleep,
        ft.elapsed,
        **kw,  # type: ignore[arg-type]
    )
    return report, journal


def test_protected_limit_rounds_toward_reference() -> None:
    # 100 * 1.002 = 100.20 -> floor to a 0.07 tick = 100.17 (never past the band)
    assert protected_limit(
        OrderSide.BUY, Decimal("100"), Decimal("20"), Decimal("0.07")
    ) == Decimal("100.17")
    # 100 * 0.998 = 99.80 -> ceil to a 0.07 tick = 99.82
    assert protected_limit(
        OrderSide.SELL, Decimal("100"), Decimal("20"), Decimal("0.07")
    ) == Decimal("99.82")


def test_marketable_limit_fills_immediately() -> None:
    venue = FakeVenue({"BTCUSDT": Decimal("50000")})
    report, journal = _run(venue, _order())
    assert report.filled_quantity == Decimal("0.1")
    assert venue.placed_count == 1
    placed = venue.orders["crypto-portfolio-20260101T120000Z-BTCUSDT-0"]
    assert placed.limit_price == Decimal("50100.00")  # 20 bps above the reference
    assert journal.entries[0][0] == "intent"  # journalled before the send


def test_partial_fills_are_requoted_with_new_client_ids() -> None:
    venue = FakeVenue({"BTCUSDT": Decimal("50000")}, fill_fraction=Decimal("0.5"))
    venue.fill_on_place = True
    report, _ = _run(venue, _order("1"))
    # attempt 0 fills 0.5 then rests (no tick), times out, cancelled; attempts 1, 2 re-quote the rest
    ids = sorted(venue.orders)
    assert [i.rsplit("-", 1)[1] for i in ids] == ["0", "1", "2"]
    assert report.filled_quantity == Decimal("0.875")
    assert all(
        o.status is BrokerOrderStatus.CANCELLED or o.status is BrokerOrderStatus.FILLED
        for o in venue.orders.values()
    )


def test_unfilled_order_is_cancelled_after_timeout_and_requoted_at_fresh_price() -> None:
    venue = FakeVenue(
        {"BTCUSDT": Decimal("51000")}
    )  # market ran away: 20 bps above 50000 doesn't cross
    prices = iter([Decimal("51000"), Decimal("51000")])
    report, _ = _run(venue, _order(), fresh_price=lambda: next(prices))
    assert report.filled_quantity == Decimal(
        "0.1"
    )  # second attempt re-quoted from 51000 and crossed
    first = venue.orders["crypto-portfolio-20260101T120000Z-BTCUSDT-0"]
    assert first.status is BrokerOrderStatus.CANCELLED


def test_lost_ack_does_not_create_a_duplicate_order() -> None:
    """Venue accepts the order, then the response is a 503. The order
    manager must look it up, find it, and NOT place it again."""
    venue = FakeVenue({"BTCUSDT": Decimal("50000")}, faults=Faults(place_lost_ack=1))
    report, _ = _run(venue, _order())
    assert venue.placed_count == 1
    assert report.filled_quantity == Decimal("0.1")
    assert any("503" in e for e in report.errors)


def test_unreachable_venue_retries_once_with_the_same_id() -> None:
    venue = FakeVenue({"BTCUSDT": Decimal("50000")}, faults=Faults(place_unreachable=1))
    report, _ = _run(venue, _order())
    assert venue.placed_count == 1
    assert list(venue.orders) == ["crypto-portfolio-20260101T120000Z-BTCUSDT-0"]
    assert report.filled_quantity == Decimal("0.1")


def test_rejection_stops_without_requoting() -> None:
    venue = FakeVenue({"BTCUSDT": Decimal("50000")}, faults=Faults(place_reject=1))
    report, journal = _run(venue, _order())
    assert venue.placed_count == 0
    assert report.filled_quantity == 0
    assert (
        "update",
        "crypto-portfolio-20260101T120000Z-BTCUSDT-0",
        "fake: insufficient balance",
    ) in journal.entries


def test_restart_resumes_an_existing_attempt_instead_of_placing_again() -> None:
    """Process restarted after placing attempt 0: the venue already has
    it, so executing the same decision again must not re-send."""
    venue = FakeVenue({"BTCUSDT": Decimal("50000")})
    _run(venue, _order())
    _run(venue, _order())
    assert venue.placed_count == 1


def test_never_reaches_venue_raises_rather_than_guessing() -> None:
    venue = FakeVenue({"BTCUSDT": Decimal("50000")}, faults=Faults(get_transient=10))
    with pytest.raises(Exception, match="could not determine state"):
        _run(venue, _order())
    assert venue.placed_count == 0
