"""The event-driven backtest engine (spec Section 4.1's flow,
end to end): bar closed -> ``Strategy.on_bar`` -> buffering -> sizing
-> order manager -> risk -> broker -> fill -> ledger, every step
logged (design rule 7).

**Fills lag decisions by exactly one bar** (design rule 3): a decision
made from bar *i*'s close becomes orders that are only filled against
bar *i+1*'s open, in the *next* loop iteration — never the same one.
Concretely: each iteration first fills whatever was decided last
iteration (against *this* bar's open), marks equity at *this* bar's
close, and only then makes a new decision (from history including
*this* bar) whose resulting orders become next iteration's fills. A
decision and the fill it causes are never in the same iteration.

Every instrument must share the same bar grid (identical
``open_time``) — a deliberate Phase 2 scope limit; nothing here
resamples or aligns mismatched calendars.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
from sqlalchemy.orm import Session

from backtest.results import BacktestResults
from core.bar import Bar
from core.clock import SimClock
from core.enums import Timeframe
from core.event_log import Event, EventType, write_events
from core.instrument import Instrument
from core.order import Order, OrderSide
from core.position import Position
from core.reproducibility import git_sha, hash_config, hash_dataframe
from execution.brokers.sim import SimBroker
from execution.costs import CostModel
from execution.order_manager import propose_orders
from portfolio.construction import implied_weight, target_quantity
from portfolio.sizing import should_trade
from research import stats as stats_module
from risk.kill_switch import KillSwitchState
from risk.kill_switch import evaluate as evaluate_kill_switch
from risk.limits import RiskLimits
from risk.pre_trade import evaluate_order
from strategies.base import Strategy, StrategyContext


def _trailing_volatility(close: pd.Series, vol_span_bars: int) -> pd.Series:
    """EWM vol of returns, shifted one bar: vol used to price a fill at
    bar *i*'s open is computed from returns through bar *i-1* only —
    never from the bar the fill happens in."""
    returns = close.pct_change()
    vol = returns.ewm(span=vol_span_bars, min_periods=vol_span_bars).std()
    return vol.shift(1).fillna(0.0)


def _make_bar(row: pd.Series, instrument: Instrument, timeframe: Timeframe) -> Bar:
    return Bar(
        venue=instrument.venue,
        symbol=instrument.symbol,
        timeframe=timeframe,
        open_time=row["open_time"],
        close_time=row["close_time"],
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row["volume"]),
    )


def run_backtest(
    strategy: Strategy,
    strategy_name: str,
    sleeve_name: str,
    prices: dict[str, pd.DataFrame],
    instruments: dict[str, Instrument],
    timeframe: Timeframe,
    initial_capital: Decimal,
    cost_models: dict[str, CostModel],
    risk_limits: RiskLimits,
    periods_per_year: float,
    run_id: str,
    buffer_pct: float = 0.10,
    participation_cap: Decimal = Decimal("0.05"),
    vol_span_days: float = 35.0,
    bars_per_day: float = 1.0,
    warmup_bars: int = 0,
    db_session: Session | None = None,
    config_for_hash: dict[str, object] | None = None,
) -> BacktestResults:
    instrument_ids = list(prices)
    first = prices[instrument_ids[0]]
    for iid in instrument_ids[1:]:
        if not prices[iid]["open_time"].equals(first["open_time"]):
            raise ValueError(
                f"{iid} bars aren't aligned with {instrument_ids[0]} — the "
                "engine requires every instrument on the same bar grid"
            )
    n_bars = len(first)
    if n_bars <= warmup_bars + 1:
        raise ValueError(
            f"only {n_bars} bars available, need more than warmup_bars+1={warmup_bars + 1}"
        )

    vol_span_bars = max(2, round(vol_span_days * bars_per_day))
    trailing_vol = {
        iid: _trailing_volatility(prices[iid]["close"], vol_span_bars) for iid in instrument_ids
    }

    symbols = {iid: instruments[iid].symbol for iid in instrument_ids}
    step_sizes = {iid: (instruments[iid].step_size or Decimal(0)) for iid in instrument_ids}
    min_notionals = {iid: (instruments[iid].min_notional or Decimal(0)) for iid in instrument_ids}
    brokers = {
        iid: SimBroker(cost_model=cost_models[iid], participation_cap=participation_cap)
        for iid in instrument_ids
    }

    clock = SimClock(first["open_time"].iloc[warmup_bars])
    positions = {iid: Position.flat(iid) for iid in instrument_ids}
    cash = initial_capital

    equity_values: list[float] = []
    equity_index: list[pd.Timestamp] = []
    held_weights_history: list[dict[str, float]] = []

    kill_switch = KillSwitchState()
    equity_peak = float(initial_capital)
    equity_today_open = float(initial_capital)
    current_day: pd.Timestamp | None = None

    events: list[Event] = []
    n_orders = n_fills = n_rejected = 0
    pending_orders: list[Order] = []

    for i in range(warmup_bars, n_bars):
        bar_close_ts = first["close_time"].iloc[i]
        clock.advance_to(bar_close_ts)

        # Phase 1: fill what was decided last iteration, against *this* bar's open.
        for order in pending_orders:
            iid = order.instrument_id
            bar = _make_bar(prices[iid].iloc[i], instruments[iid], timeframe)
            bar_volatility = Decimal(str(trailing_vol[iid].iloc[i]))
            fill = brokers[iid].submit_order(order, bar, bar_volatility)
            if fill is None:
                continue
            positions[iid] = positions[iid].apply_fill(fill)
            cash += (
                -fill.notional - fill.fee
                if fill.side == OrderSide.BUY
                else fill.notional - fill.fee
            )
            n_fills += 1
            events.append(
                Event(
                    run_id,
                    sleeve_name,
                    EventType.FILL,
                    bar_close_ts,
                    {
                        "client_order_id": fill.client_order_id,
                        "instrument_id": fill.instrument_id,
                        "side": fill.side.value,
                        "quantity": str(fill.quantity),
                        "price": str(fill.price),
                        "fee": str(fill.fee),
                    },
                )
            )
        pending_orders = []

        # Mark equity at this bar's close.
        last_prices = {iid: Decimal(str(prices[iid]["close"].iloc[i])) for iid in instrument_ids}
        equity_now = float(cash) + sum(
            float(positions[iid].quantity) * float(last_prices[iid]) for iid in instrument_ids
        )
        bar_day = bar_close_ts.normalize()
        if current_day is None or bar_day != current_day:
            current_day = bar_day
            equity_today_open = equity_now
        equity_peak = max(equity_peak, equity_now)
        equity_values.append(equity_now)
        equity_index.append(bar_close_ts)
        held_weights_history.append(
            {
                iid: implied_weight(
                    positions[iid].quantity, last_prices[iid], Decimal(str(equity_now))
                )
                for iid in instrument_ids
            }
        )
        events.append(
            Event(run_id, sleeve_name, EventType.BAR, bar_close_ts, {"equity": equity_now})
        )

        kill_switch = evaluate_kill_switch(
            kill_switch,
            equity_today_open=equity_today_open,
            equity_peak=equity_peak,
            equity_now=equity_now,
            now=bar_close_ts,
            daily_loss_limit_pct=risk_limits.daily_loss_limit_pct,
            drawdown_limit_pct=risk_limits.drawdown_limit_pct,
        )
        if kill_switch.tripped:
            events.append(
                Event(
                    run_id,
                    sleeve_name,
                    EventType.KILL_SWITCH,
                    bar_close_ts,
                    {"reason": kill_switch.reason},
                )
            )
            continue  # halt new positions; existing ones just ride, no new decision this bar

        # Phase 2: decide from history through this bar; orders fill next iteration.
        history = {
            iid: prices[iid]
            .iloc[: i + 1]
            .set_index("open_time")[["open", "high", "low", "close", "volume"]]
            for iid in instrument_ids
        }
        ctx = StrategyContext(history=history, now=bar_close_ts)
        target_weights = strategy.on_bar(ctx)
        events.append(
            Event(run_id, sleeve_name, EventType.TARGET, bar_close_ts, {"weights": target_weights})
        )

        sleeve_equity = Decimal(str(equity_now))
        buffered_weights: dict[str, float] = {}
        target_quantities: dict[str, Decimal] = {}
        held_quantities: dict[str, Decimal] = {}
        for iid in instrument_ids:
            held_qty = positions[iid].quantity
            current_weight = implied_weight(held_qty, last_prices[iid], sleeve_equity)
            raw_target = target_weights.get(iid, 0.0)
            if should_trade(current_weight, raw_target, buffer_pct):
                buffered_weights[iid] = raw_target
                target_quantities[iid] = target_quantity(
                    raw_target, sleeve_equity, last_prices[iid], step_sizes[iid]
                )
            else:
                # Not trading: keep the EXACT held quantity, not a
                # quantity reconstructed from current_weight (a float
                # derived from held_qty). That round trip isn't exactly
                # idempotent after Decimal rounding, so reconstructing
                # it was producing a spurious few-step difference from
                # held every bar -- proposed as an order, then rejected
                # for being below minimum notional, forever. Found by
                # running the real engine on real BTC/ETH data: 1731 of
                # 1754 proposed orders were exactly this, not a genuine
                # risk breach.
                buffered_weights[iid] = current_weight
                target_quantities[iid] = held_qty
            held_quantities[iid] = positions[iid].quantity

        proposed = propose_orders(
            target_quantities, held_quantities, symbols, sleeve_name, strategy_name, bar_close_ts
        )

        approved_orders = []
        for order in proposed:
            n_orders += 1
            decision = evaluate_order(
                order=order,
                reference_price=last_prices[order.instrument_id],
                step_size=step_sizes[order.instrument_id],
                min_notional=min_notionals[order.instrument_id],
                proposed_weights=buffered_weights,
                sleeve_equity=sleeve_equity,
                last_trade_price=last_prices[order.instrument_id],
                limits=risk_limits,
            )
            events.append(
                Event(
                    run_id,
                    sleeve_name,
                    EventType.RISK_DECISION,
                    bar_close_ts,
                    {
                        "client_order_id": order.client_order_id,
                        "approved": decision.approved,
                        "reason": decision.reason,
                    },
                )
            )
            if decision.approved:
                approved_orders.append(order)
                events.append(
                    Event(
                        run_id,
                        sleeve_name,
                        EventType.ORDER,
                        bar_close_ts,
                        {
                            "client_order_id": order.client_order_id,
                            "instrument_id": order.instrument_id,
                            "side": order.side.value,
                            "quantity": str(order.quantity),
                        },
                    )
                )
            else:
                n_rejected += 1

        if i + 1 < n_bars:
            pending_orders = approved_orders
        # else: this is the last bar -- nothing left to fill against, so
        # these approved orders are simply never submitted.

    if db_session is not None:
        write_events(db_session, events)

    equity_curve = pd.Series(equity_values, index=pd.DatetimeIndex(equity_index))
    returns = equity_curve.pct_change().fillna(0.0)
    weights_df = pd.DataFrame(held_weights_history, index=equity_curve.index)
    summary = stats_module.summarize(
        equity_curve, returns, weights_df, pd.Series(dtype=float), periods_per_year
    )

    combined_close = pd.DataFrame({iid: prices[iid]["close"] for iid in instrument_ids})
    config_hash = hash_config(config_for_hash or {})

    return BacktestResults(
        run_id=run_id,
        sleeve=sleeve_name,
        strategy=strategy_name,
        equity_curve=equity_curve,
        weights=weights_df,
        summary=summary,
        n_bars=n_bars - warmup_bars,
        n_orders=n_orders,
        n_fills=n_fills,
        n_rejected_orders=n_rejected,
        kill_switch_tripped=kill_switch.tripped,
        kill_switch_reason=kill_switch.reason,
        git_sha=git_sha(),
        config_hash=config_hash,
        data_hash=hash_dataframe(combined_close),
    )
