"""Spec Section 11, "Replay": a live ``sim`` run, driven bar by bar by
the real ``run_cycle``, must reproduce the backtest engine on the same
bars — same decisions, same fills, same equity. This is the test that
makes "strategy, portfolio, risk and order-manager code is identical
across modes" a checked fact rather than a claim."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import live_helpers as h
import pandas as pd
import pytest

from backtest.engine import run_backtest
from core.enums import Timeframe
from live.sleeve_runner import run_cycle
from strategies.trend_vol_target import TrendVolTarget


def test_sim_runner_reproduces_backtest_equity_exactly(tmp_path: Path) -> None:
    data = h.bars()
    rt = h.runtime(tmp_path, data)
    for i in range(h.N):
        result = run_cycle(rt, h.at(i), h.bar_close(i))
        assert result.status == "done", (i, result)

    live_equity = pd.Series(
        {s["ts"]: float(s["equity"]) for s in rt.store.snapshots}  # type: ignore[attr-defined]
    )

    prices = {f"{s[:3]}": df.reset_index(drop=True) for s, df in data.items()}
    bt = run_backtest(
        strategy=TrendVolTarget(params=h.PARAMS),
        strategy_name="trend_vol_target",
        sleeve_name="crypto",
        prices=prices,
        instruments=h.instruments(),
        timeframe=Timeframe.D1,
        initial_capital=Decimal("10000"),
        cost_models=h.cost_models(),
        risk_limits=rt.sleeve.risk.to_risk_limits(),
        periods_per_year=365,
        run_id="parity",
        buffer_pct=rt.sleeve.buffer_pct,
        participation_cap=rt.sleeve.participation_cap,
        vol_span_days=35,
        bars_per_day=1,
    )
    assert bt.n_fills > 5  # the comparison means something only if it traded
    assert len(live_equity) == len(bt.equity_curve)
    for ts, v in bt.equity_curve.items():
        assert live_equity[ts] == pytest.approx(v, rel=1e-9), ts
    fills = [e for e in rt.store.event_log if e.event_type.value == "fill"]  # type: ignore[attr-defined]
    assert len(fills) == bt.n_fills
