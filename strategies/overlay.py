"""Overlay of strategies within a sleeve (spec Section 5): e.g. 70%
trend + 30% cross-sectional momentum on equities. The combined target
weight per asset is the allocation-weighted sum of each strategy's
targets; the portfolio layer then constrains, buffers and sizes the
*combined* target, so two strategies that disagree net off instead of
trading against each other.
"""

from __future__ import annotations

from dataclasses import dataclass

from strategies.base import Strategy, StrategyContext

# Strategies whose on_bar carries incremental state (see strategies/base.py):
# a restarted live process would silently lose it, so they're backtest/sim
# research tools only and can't be configured as a live sleeve's strategy.
STATEFUL = frozenset({"buy_and_hold", "mean_reversion"})


@dataclass
class CombinedStrategy:
    components: list[tuple[float, Strategy]]

    def on_bar(self, ctx: StrategyContext) -> dict[str, float]:
        total: dict[str, float] = {aid: 0.0 for aid in ctx.history}
        for weight, strategy in self.components:
            for aid, w in strategy.on_bar(ctx).items():
                total[aid] = total.get(aid, 0.0) + weight * w
        return total
