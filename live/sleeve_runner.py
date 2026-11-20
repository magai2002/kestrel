"""One trading cycle for one sleeve in one mode (``sim``, ``sandbox``,
``live``) — the live counterpart of one iteration of
``backtest/engine.py``'s loop, built from the same parts
(``portfolio.rebalance.plan_rebalance``, the same cost/fill models in
``sim``), so a live day can be replayed through the backtester and
match.

A cycle, in order:

1. **Bars.** Fetch from the feed, persist to the store, rebuild history
   (own venue + backfill). Confirm the newest *closed* bar is the one we
   expected; if not, report ``waiting`` so the scheduler retries. Count
   staleness (in bars for crypto, in sessions for exchange calendars).
2. **Settle the previous decision.** ``sim``: fill last cycle's pending
   orders at the open of the bar right after the decision bar (exactly
   the backtest's one-bar lag). ``sandbox``/``live``: look up every
   order still unfinished in the ``orders`` table, cancel anything left
   resting, and book its fills (deduplicated on the venue's fill id, so
   a restart can never double-book).
3. **Reconcile** ledger vs venue (``sandbox``/``live``).
4. **Mark equity** at the bar's close; evaluate all six kill switches.
   Any trip: persist, cancel open orders, alert, skip the decision.
   Tripped switches stay tripped until ``kestrel admin reset-kill-switch``.
5. **Decide** (same function as the backtest) and **execute**: ``sim``
   stores approved orders as pending; ``sandbox``/``live`` send
   marketable limits through ``execution.live_execution``.
6. **Persist** the ledger, an equity/positions snapshot and every event.

Nothing here reads the system clock: ``now`` comes from the caller
(design rule 2), ``sleep``/``elapsed`` are injected.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pandas as pd
import structlog

from backtest import inputs
from config.sleeve import Listing, SleeveConfig
from core.bar import Bar
from core.event_log import Event, EventType
from core.instrument import Instrument
from core.order import Order, OrderSide
from execution.brokers.base import BrokerFill, BrokerOrder, LiveBroker, TransientBrokerError
from execution.brokers.sim import SimBroker
from execution.costs import CostModel
from execution.live_execution import ExecutionPolicy, execute_marketable_limit, protected_limit
from live.feeds import Feed, fetch_and_persist
from live.reconcile import reconcile
from live.schedule import sessions_between
from live.state import FillRow, OrderRow, SleeveState, StateStore
from live.trace import decision_trace, risk_snapshot
from observability.alerts import Alerter
from portfolio.construction import implied_weight
from portfolio.rebalance import plan_rebalance
from research.tables import clean
from risk import kill_switch as ks
from strategies.base import Strategy, StrategyContext

log = structlog.get_logger(__name__)


@dataclass
class CycleResult:
    status: str  # done | waiting | no_new_bar | halted | paused
    bar_close: datetime | None = None
    equity: Decimal | None = None
    orders: int = 0
    fills: int = 0
    tripped: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class SleeveRuntime:
    mode: str  # sim | sandbox | live
    sleeve: SleeveConfig
    strategy: Strategy
    strategy_label: str
    feed: Feed
    store: StateStore
    alerter: Alerter
    data_dir: Path
    instruments: dict[str, Instrument]
    cost_models: dict[str, CostModel]
    run_id: str
    broker: LiveBroker | None = None  # None in sim
    policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    # Real time by default. A frozen `elapsed` would make an order that
    # never fills poll forever; tests inject a fake clock explicitly.
    sleep: Callable[[float], None] = time.sleep
    elapsed: Callable[[], float] = time.monotonic
    quote_currency: str = "USD"
    history_bars: int = 3000  # how much stored history a decision sees
    on_cycle: Callable[[SleeveRuntime, CycleResult, dict[str, Any]], None] | None = None
    # Live only (Phase 7): ledger seeded with, and sizing capped at, this
    # capital; plus an absolute-currency daily loss limit.
    capital_cap: Decimal | None = None
    daily_loss_limit_abs: Decimal | None = None

    # -- small helpers -------------------------------------------------------
    @property
    def name(self) -> str:
        return self.sleeve.name

    def listing(self, aid: str) -> Listing:
        return self.sleeve.asset(aid).listing_for(self.mode)

    def symbols(self) -> dict[str, str]:
        return {a: self.listing(a).symbol for a in self.sleeve.asset_ids}

    def event(self, etype: EventType, ts: datetime, payload: dict[str, Any]) -> Event:
        return Event(self.run_id, self.name, etype, ts, payload, mode=self.mode)

    def alert(self, title: str, message: str, key: str, priority: str = "high") -> None:
        self.alerter.send(
            f"[{self.mode}/{self.name}] {title}",
            message,
            f"{self.mode}/{self.name}/{key}",
            priority,
        )


def _history(
    rt: SleeveRuntime, now: datetime, events: list[Event], errors: list[str]
) -> inputs.PriceSet | None:
    frames: dict[str, pd.DataFrame] = {}
    for aid in rt.sleeve.asset_ids:
        listing = rt.listing(aid)
        try:
            fetch_and_persist(rt.feed, listing, rt.sleeve.decision_bar, now, rt.data_dir)
        except Exception as exc:  # feed failure: fall back to what's stored; stale check decides
            errors.append(f"feed {rt.feed.name} {listing.symbol}: {exc}")
        df = inputs.read_listing_bars(listing, rt.sleeve, rt.data_dir)
        df = df[df["close_time"] <= pd.Timestamp(now)]
        if df.empty:
            return None
        frames[aid] = df.tail(rt.history_bars).reset_index(drop=True)
    return inputs.align(frames)


def _stale_reason(rt: SleeveRuntime, last_close: datetime, now: datetime) -> str | None:
    cal = rt.listing(rt.sleeve.asset_ids[0]).calendar
    max_bars = rt.sleeve.risk.stale_data_bars
    if cal is None:
        return ks.check_stale_data(
            last_close, now, rt.sleeve.decision_bar.timedelta_seconds, max_bars
        )
    delay = timedelta(seconds=rt.sleeve.bar_close_delay_seconds)
    missed = sessions_between(last_close, now - delay, cal)
    if missed >= max_bars:
        return f"{missed} {cal} sessions have closed since the latest bar ({last_close:%Y-%m-%d %H:%M})"
    return None


def _apply_fill(
    state: SleeveState, aid: str, side: OrderSide, qty: Decimal, price: Decimal, fee: Decimal
) -> None:
    pos = state.positions.get(aid, Decimal(0))
    if side is OrderSide.BUY:
        state.positions[aid] = pos + qty
        state.cash -= qty * price + fee
    else:
        state.positions[aid] = pos - qty
        state.cash += qty * price - fee


def _book_broker_fills(
    rt: SleeveRuntime,
    state: SleeveState,
    aid: str,
    fills: tuple[BrokerFill, ...] | list[BrokerFill],
    decision_price: Decimal | None,
    events: list[Event],
    ts: datetime,
) -> int:
    booked = 0
    for f in fills:
        row = FillRow(
            f.client_order_id,
            f.fill_id,
            aid,
            f.side.value,
            f.quantity,
            f.price,
            f.fee,
            f.timestamp,
            decision_price,
        )
        if rt.store.record_fill(rt.mode, rt.name, row):
            _apply_fill(state, aid, f.side, f.quantity, f.price, f.fee)
            booked += 1
            slip = None
            if decision_price:
                sign = 1 if f.side is OrderSide.BUY else -1
                slip = float(sign * (f.price - decision_price) / decision_price * 10_000)
            events.append(
                rt.event(
                    EventType.FILL,
                    ts,
                    {
                        "client_order_id": f.client_order_id,
                        "asset_id": aid,
                        "side": f.side.value,
                        "quantity": str(f.quantity),
                        "price": str(f.price),
                        "fee": str(f.fee),
                        "decision_price": str(decision_price) if decision_price else None,
                        "slippage_bps": slip,
                    },
                )
            )
    return booked


class _StoreJournal:
    def __init__(
        self,
        rt: SleeveRuntime,
        aid: str,
        bar_close: datetime,
        decision_price: Decimal,
        events: list[Event],
    ) -> None:
        self.rt, self.aid, self.bar_close, self.decision_price, self.events = (
            rt,
            aid,
            bar_close,
            decision_price,
            events,
        )

    def intent(
        self, client_order_id: str, order: Order, quantity: Decimal, limit_price: Decimal
    ) -> None:
        listing = self.rt.listing(self.aid)
        self.rt.store.upsert_order(
            self.rt.mode,
            OrderRow(
                client_order_id,
                self.rt.name,
                self.aid,
                listing.venue.value,
                listing.symbol,
                order.side.value,
                quantity,
                limit_price,
                self.decision_price,
                "intent",
                self.bar_close,
            ),
        )

    def update(
        self, client_order_id: str, broker_order: BrokerOrder | None, error: str | None
    ) -> None:
        listing = self.rt.listing(self.aid)
        status = broker_order.status.value if broker_order else "rejected"
        row = OrderRow(
            client_order_id,
            self.rt.name,
            self.aid,
            listing.venue.value,
            listing.symbol,
            broker_order.side.value if broker_order else "?",
            broker_order.quantity if broker_order else Decimal(0),
            broker_order.limit_price if broker_order else None,
            self.decision_price,
            status,
            self.bar_close,
            broker_order.broker_order_id if broker_order else None,
            broker_order.filled_quantity if broker_order else Decimal(0),
            broker_order.avg_fill_price if broker_order else None,
            error,
        )
        self.rt.store.upsert_order(self.rt.mode, row)
        self.events.append(
            self.rt.event(
                EventType.ACK,
                self.bar_close,
                {
                    "client_order_id": client_order_id,
                    "status": status,
                    "error": error,
                    "filled": str(row.filled_quantity),
                    "broker_order_id": row.broker_order_id,
                },
            )
        )


def _settle_live(
    rt: SleeveRuntime, state: SleeveState, events: list[Event], errors: list[str], ts: datetime
) -> int:
    """Recovery + cleanup: every order the journal says isn't finished."""
    assert rt.broker is not None
    booked = 0
    for row in rt.store.unfinished_orders(rt.mode, rt.name):
        try:
            current = rt.broker.get_order(row.client_order_id, row.symbol)
            if current is not None and not current.status.is_final:
                current = rt.broker.cancel_order(row.client_order_id, row.symbol)
        except TransientBrokerError as exc:
            errors.append(str(exc))
            continue
        if current is None:
            # The venue never saw it: a crash between journal "intent" and the send.
            rt.store.upsert_order(
                rt.mode,
                OrderRow(
                    **{
                        **row.__dict__,
                        "status": "rejected",
                        "last_error": "never reached the venue",
                    }
                ),
            )
            continue
        booked += _book_broker_fills(
            rt, state, row.asset_id, current.fills, row.decision_price, events, ts
        )
        _StoreJournal(
            rt, row.asset_id, row.bar_close_ts, row.decision_price or Decimal(0), events
        ).update(row.client_order_id, current, None)
    return booked


def _settle_sim(
    rt: SleeveRuntime, state: SleeveState, prices: inputs.PriceSet, events: list[Event]
) -> int:
    """Fill pending orders at the open of the bar after their decision bar."""
    if not state.pending_orders:
        return 0
    booked = 0
    sim = {
        aid: SimBroker(rt.cost_models[aid], rt.sleeve.participation_cap)
        for aid in rt.sleeve.asset_ids
    }
    vol_span = max(2, round(35 * rt.sleeve.bars_per_day))
    still_pending = []
    for p in state.pending_orders:
        aid = p["asset_id"]
        df = prices.bars[aid]
        decided_at = pd.Timestamp(p["bar_close"])
        nxt = df[df["open_time"] >= decided_at]
        if nxt.empty:
            still_pending.append(p)
            continue
        i = int(nxt.index[0])
        row = df.iloc[i]
        listing = rt.listing(aid)
        bar = Bar(
            venue=listing.venue,
            symbol=listing.symbol,
            timeframe=rt.sleeve.decision_bar,
            open_time=row["open_time"].to_pydatetime(),
            close_time=row["close_time"].to_pydatetime(),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        )
        vol = (
            df["close"]
            .pct_change()
            .ewm(span=vol_span, min_periods=vol_span)
            .std()
            .shift(1)
            .fillna(0.0)
        )
        order = Order(
            p["client_order_id"],
            aid,
            OrderSide(p["side"]),
            Decimal(p["quantity"]),
            decided_at.to_pydatetime(),
        )
        fill = sim[aid].submit_order(order, bar, Decimal(str(vol.iloc[i])))
        if fill is None:
            continue  # nothing filled (e.g. zero volume); diff-based OMS re-proposes next bar
        bf = BrokerFill(
            f"sim-{fill.client_order_id}",
            fill.client_order_id,
            listing.symbol,
            fill.side,
            fill.quantity,
            fill.price,
            fill.fee,
            fill.timestamp,
        )
        booked += _book_broker_fills(
            rt, state, aid, [bf], Decimal(p["decision_price"]), events, bar.close_time
        )
    state.pending_orders = still_pending
    return booked


def _cancel_all_open(rt: SleeveRuntime, errors: list[str]) -> None:
    """Kill-switch step 2 of 3 (trip -> cancel open orders -> alert)."""
    if rt.broker is None:
        return
    try:
        resting = rt.broker.open_orders(list(rt.symbols().values()))
    except TransientBrokerError as exc:
        errors.append(str(exc))
        return
    for resting_order in resting:
        try:
            rt.broker.cancel_order(resting_order.client_order_id, resting_order.symbol)
        except TransientBrokerError as exc:
            errors.append(str(exc))


def _flatten(
    rt: SleeveRuntime,
    state: SleeveState,
    last_prices: dict[str, Decimal],
    bar_close: datetime,
    events: list[Event],
    errors: list[str],
) -> int:
    """Sell every held position (kill-switch flatten option). Same
    execution path and client-id scheme as normal orders, with a
    distinct strategy label so the ids can't collide with a decision."""
    from execution.order_manager import propose_order

    booked = 0
    for aid, qty in state.positions.items():
        if qty <= 0:
            continue
        order = propose_order(
            aid, rt.listing(aid).symbol, Decimal(0), qty, rt.name, "flatten", bar_close
        )
        if order is None:
            continue
        events.append(
            rt.event(
                EventType.ORDER,
                bar_close,
                {
                    "client_order_id": order.client_order_id,
                    "asset_id": aid,
                    "side": "sell",
                    "quantity": str(qty),
                    "reason": "flatten_on_trip",
                },
            )
        )
        if rt.broker is None:
            state.pending_orders.append(
                {
                    "client_order_id": order.client_order_id,
                    "asset_id": aid,
                    "side": "sell",
                    "quantity": str(qty),
                    "bar_close": bar_close.isoformat(),
                    "decision_price": str(last_prices[aid]),
                }
            )
            continue
        inst = rt.instruments[aid]
        try:
            report = execute_marketable_limit(
                rt.broker,
                order,
                rt.listing(aid).symbol,
                last_prices[aid],
                inst.tick_size or Decimal(0),
                rt.policy,
                _StoreJournal(rt, aid, bar_close, last_prices[aid], events),
                rt.sleep,
                rt.elapsed,
            )
        except TransientBrokerError as exc:
            errors.append(str(exc))
            continue
        booked += _book_broker_fills(
            rt, state, aid, report.fills, last_prices[aid], events, bar_close
        )
    return booked


def run_cycle(
    rt: SleeveRuntime, now: datetime, expected_bar_close: datetime | None = None
) -> CycleResult:
    events: list[Event] = []
    errors: list[str] = []
    sleeve = rt.sleeve
    state, paused = rt.store.load(rt.mode, rt.name)
    if state is None:
        capital = (
            rt.capital_cap if rt.capital_cap is not None else Decimal(str(sleeve.paper_capital))
        )
        state = SleeveState.fresh(capital, sleeve.asset_ids, now)
        if rt.broker is not None:
            state.baseline = rt.broker.positions(list(rt.symbols().values()))
            events.append(
                rt.event(
                    EventType.RECONCILIATION,
                    now,
                    {"baseline": {k: str(v) for k, v in state.baseline.items()}},
                )
            )

    prices = _history(rt, now, events, errors)
    if prices is None or len(prices.close_panel) == 0:
        return CycleResult("waiting", detail="no stored history yet: " + "; ".join(errors))
    panel = prices.close_panel
    bar_close = panel.index[-1].to_pydatetime()
    if expected_bar_close is not None and bar_close < expected_bar_close:
        stale = _stale_reason(rt, bar_close, now)
        if stale is None:
            return CycleResult(
                "waiting",
                bar_close,
                detail=f"latest closed bar {bar_close}, expecting {expected_bar_close}",
            )
    if state.last_bar_close and pd.Timestamp(state.last_bar_close) >= pd.Timestamp(bar_close):
        # Nothing new to decide on -- but "nothing new for too long" is
        # exactly what the stale-data switch exists for, so judge it here
        # too (otherwise a dead feed could never trip it).
        stale = _stale_reason(rt, bar_close, now)
        already = rt.store.kill_switches(rt.mode, rt.name).get("stale_data")
        if stale is not None and not (already and already.tripped):
            rt.store.trip(rt.mode, rt.name, "stale_data", stale, now)
            rt.store.events(
                [rt.event(EventType.KILL_SWITCH, now, {"name": "stale_data", "reason": stale})]
            )
            rt.alert("KILL SWITCH stale_data", stale, "stale_data", "urgent")
            _cancel_all_open(rt, errors)
            return CycleResult("halted", bar_close, tripped=["stale_data"], detail=stale)
        return CycleResult("no_new_bar", bar_close)

    booked = 0
    if rt.broker is None:
        booked += _settle_sim(rt, state, prices, events)
    else:
        booked += _settle_live(rt, state, events, errors, bar_close)

    last_prices = {aid: Decimal(str(panel[aid].iloc[-1])) for aid in sleeve.asset_ids}
    switches = rt.store.kill_switches(rt.mode, rt.name)
    new_trips: dict[str, str] = {}

    # --- reconcile ---
    if rt.broker is not None:
        try:
            venue_pos = rt.broker.positions(list(rt.symbols().values()))
            venue_cash = rt.broker.cash(rt.quote_currency)
            steps = {a: rt.instruments[a].step_size or Decimal(0) for a in sleeve.asset_ids}
            rec = reconcile(
                state.positions,
                rt.symbols(),
                state.baseline,
                venue_pos,
                steps,
                sleeve.risk.reconcile_tolerance_pct,
                state.cash,
                venue_cash,
            )
            events.append(
                rt.event(
                    EventType.RECONCILIATION,
                    bar_close,
                    {
                        "ok": rec.ok,
                        "mismatches": rec.mismatches,
                        "venue": {k: str(v) for k, v in rec.venue_positions.items()},
                        "expected": {k: str(v) for k, v in rec.expected_positions.items()},
                        "venue_cash": str(venue_cash),
                        "ledger_cash": str(state.cash),
                    },
                )
            )
            if r := ks.check_reconcile(rec.mismatches):
                new_trips["reconcile_mismatch"] = r
            if r := ks.check_clock_drift(
                rt.broker.server_time(), now, sleeve.risk.clock_drift_seconds
            ):
                new_trips["clock_drift"] = r
        except TransientBrokerError as exc:
            errors.append(str(exc))

    # --- mark equity ---
    equity = state.cash + sum(
        (state.positions[a] * last_prices[a] for a in sleeve.asset_ids), Decimal(0)
    )
    eq_f = float(equity)
    day = bar_close.date().isoformat()
    if state.day != day:
        # Measured from the last mark before the day began (yesterday's
        # close), exactly like backtest/engine.py -- see the note there for
        # the daily-bar bug this fixes.
        prev = state.last_equity if state.last_equity is not None else float(state.initial_capital)
        state.day, state.day_open_equity = day, prev
    state.last_equity = eq_f
    state.equity_peak = max(state.equity_peak, eq_f)
    if r := ks.check_daily_loss(state.day_open_equity, eq_f, sleeve.risk.daily_loss_limit_pct):
        new_trips["daily_loss"] = r
    if rt.daily_loss_limit_abs is not None:  # live only (Phase 7 gate 6): currency, not just %
        lost = Decimal(str(state.day_open_equity)) - equity
        if lost > rt.daily_loss_limit_abs:
            new_trips["daily_loss"] = (
                f"daily loss {lost:.2f} {sleeve.base_currency} exceeds absolute limit "
                f"{rt.daily_loss_limit_abs}"
            )
    if r := ks.check_drawdown(state.equity_peak, eq_f, sleeve.risk.drawdown_limit_pct):
        new_trips["drawdown"] = r
    if r := _stale_reason(rt, bar_close, now):
        new_trips["stale_data"] = r
    state.error_times = [
        t
        for t in state.error_times
        if (now - datetime.fromisoformat(t)).total_seconds()
        <= sleeve.risk.error_burst_window_minutes * 60
    ]
    state.error_times += [now.isoformat()] * len(errors)
    if r := ks.check_error_burst(
        [datetime.fromisoformat(t) for t in state.error_times],
        now,
        sleeve.risk.error_burst_count,
        sleeve.risk.error_burst_window_minutes,
    ):
        new_trips["error_burst"] = r

    for name, reason in new_trips.items():
        if not switches.get(name, ks.KillSwitchState()).tripped:
            rt.store.trip(rt.mode, rt.name, name, reason, now)
            events.append(
                rt.event(EventType.KILL_SWITCH, bar_close, {"name": name, "reason": reason})
            )
            rt.alert(f"KILL SWITCH {name}", reason, name, "urgent")
    tripped = sorted(n for n, s in rt.store.kill_switches(rt.mode, rt.name).items() if s.tripped)

    weights_now = {
        a: implied_weight(state.positions[a], last_prices[a], equity) for a in sleeve.asset_ids
    }
    snapshot_positions = {
        a: {
            "quantity": str(state.positions[a]),
            "price": str(last_prices[a]),
            "weight": weights_now[a],
        }
        for a in sleeve.asset_ids
    }
    rt.store.snapshot(
        rt.mode,
        rt.name,
        bar_close,
        equity,
        state.cash,
        eq_f / float(state.initial_capital) * 100.0,
        eq_f / state.equity_peak - 1.0,
        sum(abs(w) for w in weights_now.values()),
        snapshot_positions,
    )
    events.append(
        rt.event(
            EventType.BAR, bar_close, {"equity": eq_f, "cash": str(state.cash), "errors": errors}
        )
    )

    def finish(status: str, orders: int = 0, detail: str = "") -> CycleResult:
        state.last_bar_close = bar_close.isoformat()
        rt.store.save(rt.mode, rt.name, state, now)
        rt.store.events(events)
        result = CycleResult(status, bar_close, equity, orders, booked, tripped, detail)
        if rt.on_cycle is not None:
            rt.on_cycle(rt, result, {"weights": weights_now, "prices": last_prices, "state": state})
        log.info(
            "cycle",
            mode=rt.mode,
            sleeve=rt.name,
            status=status,
            bar_close=bar_close.isoformat(),
            equity=eq_f,
            orders=orders,
            fills=booked,
            tripped=tripped,
            errors=errors,
        )
        return result

    def trace(status: str, extra: dict[str, Any] | None = None, **kw: Any) -> None:
        """Decision-trace event (dashboard only; a failure here is logged, never raised)."""
        try:
            payload: dict[str, Any] = {
                "status": status,
                "tripped": tripped,
                "risk": risk_snapshot(rt, state, eq_f, weights_now, bar_close, now, **kw),
            }
            payload.update(extra or {})
            payload = cast(dict[str, Any], clean(payload))  # JSON-safe: no NaN into Postgres
        except Exception as exc:
            log.warning("decision_trace_failed", sleeve=rt.name, error=str(exc))
            payload = {"status": status, "tripped": tripped, "error": str(exc)}
        events.append(rt.event(EventType.DECISION, bar_close, payload))

    if tripped:
        trace("halted")
        _cancel_all_open(rt, errors)
        state.pending_orders = []
        flattened = 0
        if sleeve.risk.flatten_on_trip and new_trips:
            # ADR 0005: off by default (cancel and HOLD). When enabled,
            # flatten once, on the cycle the switch trips -- never on
            # every later halted cycle.
            flattened = _flatten(rt, state, last_prices, bar_close, events, errors)
            booked += flattened
        return finish("halted", detail="kill switch(es) tripped: " + ", ".join(tripped))
    if paused:
        trace("paused")
        state.pending_orders = []
        return finish("paused")

    # --- decide (the same function the backtest engine calls) ---
    history = {
        aid: prices.bars[aid].set_index("open_time")[["open", "high", "low", "close", "volume"]]
        for aid in sleeve.asset_ids
    }
    ctx = StrategyContext(history=history, now=bar_close)
    raw = rt.strategy.on_bar(ctx)
    sizing_equity = equity if rt.capital_cap is None else min(equity, rt.capital_cap)
    # The price band is checked against the last price; the live limit
    # sits protection_bps (0.2%) from it, well inside the 1-2% band.
    plan = plan_rebalance(
        raw_target_weights=raw,
        held_quantities=dict(state.positions),
        last_prices=last_prices,
        # Live (gate 2): never size from more than max_live_capital.
        sleeve_equity=sizing_equity,
        symbols=rt.symbols(),
        step_sizes={a: rt.instruments[a].step_size or Decimal(0) for a in sleeve.asset_ids},
        min_notionals={a: rt.instruments[a].min_notional or Decimal(0) for a in sleeve.asset_ids},
        risk_limits=sleeve.risk.to_risk_limits(),
        buffer_pct=sleeve.buffer_pct,
        sleeve=rt.name,
        strategy=rt.strategy_label,
        bar_close_ts=bar_close,
    )
    events.append(
        rt.event(
            EventType.TARGET,
            bar_close,
            {"raw": raw, "weights": plan.target_weights, "buffered": plan.buffered_weights},
        )
    )
    trace(
        "decided",
        decision_trace(rt, ctx, raw, plan, weights_now, last_prices, sizing_equity),
        after=plan.buffered_weights,
    )
    for order, decision in plan.decisions:
        events.append(
            rt.event(
                EventType.RISK_DECISION,
                bar_close,
                {
                    "client_order_id": order.client_order_id,
                    "approved": decision.approved,
                    "reason": decision.reason,
                },
            )
        )

    if rt.broker is None:
        state.pending_orders = [
            {
                "client_order_id": o.client_order_id,
                "asset_id": o.instrument_id,
                "side": o.side.value,
                "quantity": str(o.quantity),
                "bar_close": bar_close.isoformat(),
                "decision_price": str(last_prices[o.instrument_id]),
            }
            for o in plan.approved
        ]
        for o in plan.approved:
            events.append(
                rt.event(
                    EventType.ORDER,
                    bar_close,
                    {
                        "client_order_id": o.client_order_id,
                        "asset_id": o.instrument_id,
                        "side": o.side.value,
                        "quantity": str(o.quantity),
                        "fills_at": "next bar open",
                    },
                )
            )
        return finish("done", len(plan.approved))

    # sandbox / live: sells first (they free the cash buys need)
    for order in sorted(plan.approved, key=lambda o: o.side is OrderSide.BUY):
        aid = order.instrument_id
        inst = rt.instruments[aid]
        events.append(
            rt.event(
                EventType.ORDER,
                bar_close,
                {
                    "client_order_id": order.client_order_id,
                    "asset_id": aid,
                    "side": order.side.value,
                    "quantity": str(order.quantity),
                    "limit": str(
                        protected_limit(
                            order.side,
                            last_prices[aid],
                            rt.policy.protection_bps,
                            inst.tick_size or Decimal(0),
                        )
                    ),
                },
            )
        )
        symbol = rt.listing(aid).symbol
        broker = rt.broker

        def fresh(b: LiveBroker = broker, s: str = symbol) -> Decimal | None:
            try:
                return b.last_price(s)
            except TransientBrokerError:
                return None

        try:
            report = execute_marketable_limit(
                broker,
                order,
                symbol,
                last_prices[aid],
                inst.tick_size or Decimal(0),
                rt.policy,
                _StoreJournal(rt, aid, bar_close, last_prices[aid], events),
                rt.sleep,
                rt.elapsed,
                min_quantity=inst.step_size or Decimal(0),
                fresh_price=fresh,
            )
        except TransientBrokerError as exc:
            errors.append(str(exc))
            continue
        errors.extend(report.errors)
        booked += _book_broker_fills(
            rt, state, aid, report.fills, last_prices[aid], events, bar_close
        )
    return finish("done", len(plan.approved))


def rebaseline(rt: SleeveRuntime, now: datetime) -> dict[str, str]:
    """Admin: accept the venue's current holdings as the new baseline
    (after a testnet wipe or a deliberate manual change). Ledger
    positions are kept; baseline = venue - ledger."""
    assert rt.broker is not None
    state, _ = rt.store.load(rt.mode, rt.name)
    if state is None:
        raise KeyError("no state yet")
    venue = rt.broker.positions(list(rt.symbols().values()))
    sym = rt.symbols()
    state.baseline = {
        sym[a]: venue.get(sym[a], Decimal(0)) - state.positions[a] for a in rt.sleeve.asset_ids
    }
    rt.store.save(rt.mode, rt.name, state, now)
    return {k: str(v) for k, v in state.baseline.items()}
