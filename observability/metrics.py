"""Prometheus metrics (spec Section 9), served by the engine process.

Names and labels are exactly the spec's. ``start()`` binds to
``metrics_addr`` (``127.0.0.1`` by default; the container sets
``0.0.0.0`` *inside* its network namespace and Compose publishes the
port on ``127.0.0.1`` only — ADR 0003).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from prometheus_client import Counter, Gauge, Histogram, start_http_server

EQUITY_INDEX = Gauge(
    "kestrel_equity_index", "Sleeve equity, indexed to 100 at start", ["sleeve", "mode"]
)
DRAWDOWN = Gauge("kestrel_drawdown", "Drawdown from peak (negative fraction)", ["sleeve", "mode"])
POSITION_WEIGHT = Gauge(
    "kestrel_position_weight", "Held weight per asset", ["sleeve", "symbol", "mode"]
)
ORDERS = Counter("kestrel_orders_total", "Orders by final status", ["sleeve", "status", "mode"])
ORDER_LATENCY = Histogram(
    "kestrel_order_latency_seconds",
    "Decision to final order state",
    ["sleeve", "mode"],
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300),
)
SLIPPAGE = Histogram(
    "kestrel_slippage_bps",
    "Fill vs decision price, adverse positive",
    ["sleeve", "mode"],
    buckets=(-50, -20, -10, -5, 0, 5, 10, 20, 50, 100, 200),
)
LAST_BAR = Gauge(
    "kestrel_last_bar_close_timestamp",
    "Close time of the latest processed bar",
    ["sleeve", "symbol", "mode"],
)
KILL_SWITCH = Gauge("kestrel_kill_switch_tripped", "1 if tripped", ["sleeve", "name", "mode"])
RECONCILE_MISMATCH = Gauge(
    "kestrel_reconcile_mismatch", "1 if the last reconciliation mismatched", ["sleeve", "mode"]
)
LAST_SUCCESS = Gauge(
    "kestrel_last_success_unixtime", "Last successful run of a component", ["component"]
)


@dataclass
class Hooks:
    def on_cycle(self, rt: Any, result: Any, extra: dict[str, Any]) -> None:
        from risk.kill_switch import KILL_SWITCH_NAMES

        mode, sleeve = rt.mode, rt.name
        state = extra["state"]
        if result.equity is not None:
            EQUITY_INDEX.labels(sleeve, mode).set(
                float(result.equity) / float(state.initial_capital) * 100
            )
            DRAWDOWN.labels(sleeve, mode).set(float(result.equity) / state.equity_peak - 1.0)
        for aid, w in extra["weights"].items():
            POSITION_WEIGHT.labels(sleeve, aid, mode).set(w)
            if result.bar_close is not None:
                LAST_BAR.labels(sleeve, aid, mode).set(result.bar_close.timestamp())
        tripped = set(result.tripped)
        for name in KILL_SWITCH_NAMES:
            KILL_SWITCH.labels(sleeve, name, mode).set(1 if name in tripped else 0)
        RECONCILE_MISMATCH.labels(sleeve, mode).set(1 if "reconcile_mismatch" in tripped else 0)
        if result.orders:
            ORDERS.labels(sleeve, "submitted", mode).inc(result.orders)
        if result.fills:
            ORDERS.labels(sleeve, "filled", mode).inc(result.fills)
        LAST_SUCCESS.labels(f"cycle:{mode}:{sleeve}").set_to_current_time()

    def heartbeat(self, now: datetime) -> None:
        LAST_SUCCESS.labels("engine").set(now.timestamp())


def start(port: int, addr: str = "127.0.0.1") -> Hooks:
    start_http_server(port, addr=addr)
    return Hooks()
