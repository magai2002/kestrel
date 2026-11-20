"""The order manager's live path (spec Phase 4): **marketable limit
orders with price protection, a timeout, then cancel and re-quote**.

Why not plain market orders? A market order on a thin book (testnet;
an LSE USD-line ETF at 08:01) can fill arbitrarily far from the last
price. A *marketable limit* — a buy limit a little above the last
price, a sell a little below — crosses the spread like a market order
would, but can never fill worse than ``protection_bps`` away from the
reference. If it hasn't filled by the timeout (the book moved away),
cancel it and re-quote from a fresh price, up to ``max_requotes`` times.
Whatever is still unfilled after that is simply left: the diff-based
order manager proposes the remaining gap again next bar (design rule 1).

**Idempotency and the "5xx that wasn't a failure" problem.** Every
attempt has its own deterministic client id (``...-{seq}``, seq =
attempt). Before placing an attempt we *ask the venue* whether it
already has that id — so a process killed between "send" and "ack"
resumes watching the order it already placed instead of placing a
second one. A ``TransientBrokerError`` on place means "unknown": we
look the id up (with retries) before deciding whether to place again.

Every state change goes through ``journal`` (the ``orders`` table in
live, a list in tests) *before* the network call that causes it, so a
crash at any point leaves a row saying what we were about to do.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Protocol

from core.order import Order, OrderSide
from execution.brokers.base import (
    BrokerError,
    BrokerFill,
    BrokerOrder,
    BrokerOrderStatus,
    LiveBroker,
    TransientBrokerError,
)
from execution.brokers.client_ids import attempt_client_id


@dataclass(frozen=True)
class ExecutionPolicy:
    protection_bps: Decimal = Decimal("20")
    timeout_seconds: float = 60.0
    poll_seconds: float = 2.0
    max_requotes: int = 2
    lookup_retries: int = 3


class Journal(Protocol):
    def intent(
        self, client_order_id: str, order: Order, quantity: Decimal, limit_price: Decimal
    ) -> None:
        """About to send this attempt."""
        ...

    def update(
        self, client_order_id: str, broker_order: BrokerOrder | None, error: str | None
    ) -> None: ...


@dataclass
class ListJournal:
    entries: list[tuple[str, str, object]] = field(default_factory=list)

    def intent(
        self, client_order_id: str, order: Order, quantity: Decimal, limit_price: Decimal
    ) -> None:
        self.entries.append(("intent", client_order_id, (quantity, limit_price)))

    def update(
        self, client_order_id: str, broker_order: BrokerOrder | None, error: str | None
    ) -> None:
        self.entries.append(
            ("update", client_order_id, broker_order.status if broker_order else error)
        )


@dataclass(frozen=True)
class ExecutionReport:
    order: Order
    attempts: tuple[BrokerOrder, ...]
    fills: tuple[BrokerFill, ...]
    errors: tuple[str, ...]

    @property
    def filled_quantity(self) -> Decimal:
        return sum((f.quantity for f in self.fills), Decimal(0))


def protected_limit(
    side: OrderSide, reference: Decimal, protection_bps: Decimal, tick: Decimal
) -> Decimal:
    """Buy: reference * (1 + bps), rounded *down* to tick; sell: reference
    * (1 - bps), rounded *up*. Rounding toward the reference keeps the
    price inside the protection band, never one tick past it."""
    factor = protection_bps / Decimal(10_000)
    raw = reference * (1 + factor) if side is OrderSide.BUY else reference * (1 - factor)
    if tick <= 0:
        return raw
    rounding = ROUND_FLOOR if side is OrderSide.BUY else ROUND_CEILING
    return (raw / tick).to_integral_value(rounding=rounding) * tick


def _lookup(
    broker: LiveBroker, cid: str, symbol: str, retries: int, errors: list[str]
) -> BrokerOrder | None:
    """``None`` only once the venue has *definitely* said it doesn't know
    the id; raises if it never answered (we must not guess)."""
    last_exc: Exception | None = None
    for _ in range(max(1, retries)):
        try:
            return broker.get_order(cid, symbol)
        except TransientBrokerError as exc:
            errors.append(str(exc))
            last_exc = exc
    raise TransientBrokerError(f"could not determine state of {cid}: {last_exc}")


def execute_marketable_limit(
    broker: LiveBroker,
    order: Order,
    symbol: str,
    reference_price: Decimal,
    tick_size: Decimal,
    policy: ExecutionPolicy,
    journal: Journal,
    sleep: Callable[[float], None],
    elapsed: Callable[[], float],
    min_quantity: Decimal = Decimal(0),
    fresh_price: Callable[[], Decimal | None] | None = None,
) -> ExecutionReport:
    attempts: list[BrokerOrder] = []
    fills: dict[str, BrokerFill] = {}
    errors: list[str] = []
    remaining = order.quantity
    reference = reference_price

    for attempt in range(policy.max_requotes + 1):
        cid = attempt_client_id(order.client_order_id, attempt)
        current = _lookup(broker, cid, symbol, policy.lookup_retries, errors)
        if current is None:
            if remaining <= 0 or remaining < min_quantity:
                break
            price = protected_limit(order.side, reference, policy.protection_bps, tick_size)
            journal.intent(cid, order, remaining, price)
            try:
                current = broker.place_limit_order(cid, symbol, order.side, remaining, price)
            except TransientBrokerError as exc:
                errors.append(str(exc))
                current = _lookup(broker, cid, symbol, policy.lookup_retries, errors)
                if current is None:
                    # Venue definitely never saw it: one clean retry, same id.
                    try:
                        current = broker.place_limit_order(
                            cid, symbol, order.side, remaining, price
                        )
                    except BrokerError as exc2:
                        errors.append(str(exc2))
                        journal.update(cid, None, str(exc2))
                        break
            except BrokerError as exc:
                errors.append(str(exc))
                journal.update(cid, None, str(exc))
                break
        journal.update(cid, current, None)

        start = elapsed()
        while not current.status.is_final and elapsed() - start < policy.timeout_seconds:
            sleep(policy.poll_seconds)
            try:
                found = broker.get_order(cid, symbol)
            except TransientBrokerError as exc:
                errors.append(str(exc))
                continue
            if found is not None:
                current = found
        if not current.status.is_final:
            try:
                cancelled = broker.cancel_order(cid, symbol)
            except TransientBrokerError as exc:
                errors.append(str(exc))
                cancelled = _lookup(broker, cid, symbol, policy.lookup_retries, errors)
            if cancelled is not None:
                current = cancelled
        journal.update(cid, current, None)
        attempts.append(current)
        for f in current.fills:
            fills[f.fill_id] = f
        itemised = sum((f.quantity for f in current.fills), Decimal(0))
        if current.filled_quantity > itemised:
            # The venue says more filled than it itemised (trades not
            # loaded yet). Trust the filled quantity -- re-quoting it
            # would double-buy -- and flag the missing fee.
            gap = current.filled_quantity - itemised
            errors.append(f"{cid}: {gap} filled without itemised trades; fee unknown")
            fills[f"{current.broker_order_id}-agg"] = BrokerFill(
                fill_id=f"{current.broker_order_id}-agg",
                client_order_id=cid,
                symbol=symbol,
                side=order.side,
                quantity=gap,
                price=current.avg_fill_price or current.limit_price or reference,
                fee=Decimal(0),
                timestamp=order.created_at,
            )
        remaining = order.quantity - sum((f.quantity for f in fills.values()), Decimal(0))
        if current.status is BrokerOrderStatus.REJECTED or remaining <= 0:
            break
        if fresh_price is not None:
            px = fresh_price()
            if px is not None and px > 0:
                reference = px

    return ExecutionReport(order, tuple(attempts), tuple(fills.values()), tuple(errors))
