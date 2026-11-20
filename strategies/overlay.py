"""Overlay of strategies within a sleeve (spec Section 5): e.g. 70%
trend + 30% cross-sectional momentum on equities. The combined target
weight per asset is the allocation-weighted sum of each strategy's
targets; the portfolio layer then constrains, buffers and sizes the
*combined* target, so two strategies that disagree net off instead of
trading against each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from strategies.base import Strategy, StrategyContext

# Strategies whose on_bar carries incremental state (see strategies/base.py):
# a restarted live process would silently lose it, so they're backtest/sim
# research tools only and can't be configured as a live sleeve's strategy.
STATEFUL = frozenset({"buy_and_hold", "mean_reversion"})


@dataclass
class CombinedStrategy:
    components: list[tuple[float, Strategy]]
    labels: tuple[str, ...] = field(default=())

    def on_bar(self, ctx: StrategyContext) -> dict[str, float]:
        total: dict[str, float] = {aid: 0.0 for aid in ctx.history}
        for weight, strategy in self.components:
            for aid, w in strategy.on_bar(ctx).items():
                total[aid] = total.get(aid, 0.0) + weight * w
        return total


def explain_strategy(strategy: Strategy, label: str, ctx: StrategyContext) -> list[dict[str, Any]]:
    """The decision trace of every strategy in a sleeve: allocation,
    its own target weights, and (where the strategy has an ``explain``)
    the per-asset working behind them. Sum of ``allocation * weights``
    is exactly what ``on_bar`` returned."""
    if isinstance(strategy, CombinedStrategy):
        names = strategy.labels or tuple(f"strategy_{i}" for i in range(len(strategy.components)))
        parts = [(w, s, n) for (w, s), n in zip(strategy.components, names, strict=True)]
    else:
        parts = [(1.0, strategy, label)]
    out = []
    for weight, s, name in parts:
        explain = getattr(s, "explain", None)
        body: dict[str, Any] = (
            explain(ctx) if callable(explain) else {"weights": s.on_bar(ctx), "assets": {}}
        )
        out.append({"strategy": name, "allocation": weight, **body})
    return out
