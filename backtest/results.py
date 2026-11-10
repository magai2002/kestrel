"""The backtest engine's output: an equity curve plus the summary
stats from ``research/stats.py``, bundled with the reproducibility
metadata design rule 8 asks for (git SHA, config hash, data hash).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from research.stats import SummaryStats


@dataclass(frozen=True)
class BacktestResults:
    run_id: str
    sleeve: str
    strategy: str
    equity_curve: pd.Series
    weights: pd.DataFrame  # actually-held weight per instrument, per bar
    summary: SummaryStats
    n_bars: int
    n_orders: int
    n_fills: int
    n_rejected_orders: int
    kill_switch_tripped: bool
    kill_switch_reason: str | None
    git_sha: str | None
    config_hash: str
    data_hash: str

    def format_summary(self) -> str:
        lines = [
            f"Backtest: {self.strategy} on {self.sleeve} ({self.n_bars} bars)",
            f"  run_id:          {self.run_id}",
            f"  git_sha:         {self.git_sha or '(not a git checkout)'}",
            f"  config_hash:     {self.config_hash}",
            f"  data_hash:       {self.data_hash}",
            f"  CAGR:            {self.summary.cagr:+.1%}",
            f"  Annualised vol:  {self.summary.annualized_vol:.1%}",
            f"  Sharpe:          {self.summary.sharpe:.2f}",
            f"  Sortino:         {self.summary.sortino:.2f}",
            f"  Max drawdown:    {self.summary.max_drawdown:.1%} "
            f"({self.summary.max_drawdown_duration_bars} bars to recover)",
            f"  Turnover:        {self.summary.turnover:.2%}/bar",
            f"  Orders:          {self.n_orders} "
            f"({self.n_fills} filled, {self.n_rejected_orders} rejected)",
        ]
        if self.kill_switch_tripped:
            lines.append(f"  KILL SWITCH TRIPPED: {self.kill_switch_reason}")
        return "\n".join(lines)
