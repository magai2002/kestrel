"""Nightly shadow backtest (spec Section 8.1 ``/tracking``): replay the
bars a live mode actually consumed — the ones its feed persisted to the
store — through the backtest engine, over exactly the live period, with
the same strategy, costs and limits. Then compare with what the live
mode actually did.

For ``sim`` the two should agree almost exactly, by construction
(``tests/unit/live/test_replay_parity.py``). Whatever gap remains is the
point of measuring: bars the runner missed while down, kill-switch
halts and hand resets, venue data revised after the fact, restarts with
pending orders. For ``sandbox``/``live`` the gap also contains real
execution: venue fills vs the model's next-open fills.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from backtest import inputs
from backtest.engine import run_backtest
from config.settings import Settings
from config.sleeve import load_sleeve
from core.orm import BacktestRunRecord, EquitySnapshotRecord, FillRecord
from core.reproducibility import git_sha
from live.build import build_strategy


def run_shadow(
    mode: str, sleeve_name: str, settings: Settings, session: Session, now: datetime
) -> dict[str, Any]:
    sleeve = load_sleeve(sleeve_name)
    snaps = session.scalars(
        select(EquitySnapshotRecord)
        .where(EquitySnapshotRecord.mode == mode, EquitySnapshotRecord.sleeve == sleeve_name)
        .order_by(EquitySnapshotRecord.timestamp)
    ).all()
    if len(snaps) < 2:
        return {"status": "insufficient_live_history", "n_live_bars": len(snaps)}
    live_start = pd.Timestamp(snaps[0].timestamp)

    prices = inputs.load_prices(sleeve, settings.data_dir, mode=mode, end=pd.Timestamp(now))
    grid = pd.DatetimeIndex(next(iter(prices.bars.values()))["close_time"])
    start_idx = int(grid.searchsorted(live_start))
    if start_idx >= len(grid) - 1:
        return {"status": "insufficient_bars", "n_live_bars": len(snaps)}
    instruments, _ = inputs.load_instruments(sleeve, mode, session)
    strategy, label = build_strategy(sleeve)
    result = run_backtest(
        strategy=strategy,
        strategy_name=label,
        sleeve_name=sleeve_name,
        prices=prices.bars,
        instruments=instruments,
        timeframe=sleeve.decision_bar,
        initial_capital=Decimal(str(sleeve.paper_capital)),
        cost_models=inputs.cost_models(sleeve),
        risk_limits=sleeve.risk.to_risk_limits(),
        periods_per_year=sleeve.periods_per_year,
        run_id=f"shadow-{mode}-{sleeve_name}-{now:%Y%m%dT%H%M}",
        buffer_pct=sleeve.buffer_pct,
        participation_cap=sleeve.participation_cap,
        bars_per_day=sleeve.bars_per_day,
        warmup_bars=start_idx,
    )
    shadow = result.equity_curve / float(sleeve.paper_capital) * 100.0
    live = pd.Series({pd.Timestamp(s.timestamp): s.equity_index for s in snaps})
    joined = pd.DataFrame({"live": live, "shadow": shadow}).dropna()
    gap = joined["live"] - joined["shadow"]
    years = max(len(joined) / sleeve.periods_per_year, 1e-9)
    tracking = {
        "status": "ok",
        "mode": mode,
        "sleeve": sleeve_name,
        "live_start": live_start.isoformat(),
        "as_of": now.isoformat(),
        "n_bars": len(joined),
        "final_gap_points": float(gap.iloc[-1]) if len(gap) else None,
        # Annualised gap in index points ~ % of starting capital per year.
        "gap_pct_annualised": float(gap.iloc[-1] / years) if len(gap) else None,
        "max_abs_gap_points": float(gap.abs().max()) if len(gap) else None,
        "curve": [
            [ts.isoformat(), float(lv), float(sh)]
            for ts, lv, sh in zip(
                pd.DatetimeIndex(joined.index), joined["live"], joined["shadow"], strict=True
            )
        ],
        "shadow_fills": result.n_fills,
        "live_fills": session.scalar(
            select(FillRecord.id)
            .where(FillRecord.mode == mode, FillRecord.sleeve == sleeve_name)
            .limit(1)
        )
        is not None,
    }
    session.merge(
        BacktestRunRecord(
            run_id=result.run_id,
            strategy=label,
            sleeve=sleeve_name,
            kind=f"shadow:{mode}",
            git_sha=git_sha(),
            config_hash=result.config_hash,
            data_hash=result.data_hash,
            summary=tracking,
        )
    )
    session.commit()
    return tracking
