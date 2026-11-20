"""The validation pipeline (spec Section 7): hold-out lock, walk-forward
grid search, trial ledger, DSR, PBO, bootstrap, cost sensitivity,
per-year/per-regime tables, parameter-stability heatmap.

``docs/validation-methodology.md`` explains each step for a non-quant
reader; this docstring is the engineer's version.

**Order of operations, and why:**

1. Split off the most recent ``holdout_fraction`` (25%) of bars. Nothing
   in steps 2-6 ever sees them: weights are computed on the pre-hold-out
   panel only (strategies are causal, so this changes nothing *before*
   the split — the no-look-ahead property test guarantees it).
2. Every grid point is run once over the whole pre-hold-out period with
   the full cost model (``research.vectorized.run_with_costs``) and
   written to the trial ledger.
3. **Walk-forward**: rolling train/test windows (calendar days from
   ``config/validation.yaml``). In each train window, the grid point with
   the best net Sharpe *in that window* is chosen; its returns over the
   following test window are the out-of-sample (OOS) segment. Segments
   are stitched into one OOS series — the number the report leads with.
   (Slicing each grid point's one continuous run means the chosen
   strategy enters a test window already holding whatever it would have
   held; the cost of *switching* parameter sets at a boundary is not
   charged. Small, and documented.)
4. DSR on the OOS series, with the trial count and Sharpe variance from
   the ledger (all distinct grid points ever tried for this strategy and
   sleeve, not just this run's).
5. PBO via CSCV on the grid's pre-hold-out return matrix.
6. Bootstrap CI, cost sensitivity, per-year/regime, heatmap, buffering.
7. Only if asked (``evaluate_holdout=True``): pick the final params on
   the *whole* pre-hold-out period, run once on the hold-out, record the
   access. A second access marks the report contaminated.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict

from backtest import inputs
from config.sleeve import SleeveConfig
from core.reproducibility import hash_config, hash_dataframe
from portfolio.constraints import constrain_weight_frame
from research import overfitting, stats, tables, vectorized
from research.ledger import Ledger, Trial

VALIDATION_CONFIG_PATH = Path("config/validation.yaml")


class SleeveWindows(BaseModel):
    model_config = ConfigDict(frozen=True)
    train_days: int
    test_days: int


class StrategyValidation(BaseModel):
    model_config = ConfigDict(frozen=True)
    baseline: str
    warmup_days: float
    grid: dict[str, Any]


class ValidationConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    holdout_fraction: float
    bootstrap_block_days: float
    bootstrap_resamples: int
    pbo_blocks: int
    regime_sma_days: float
    regime_slope_days: float
    cost_multipliers: list[float]
    sharp_peak_threshold: float
    sleeves: dict[str, SleeveWindows]
    strategies: dict[str, StrategyValidation]


def load_validation_config(path: Path = VALIDATION_CONFIG_PATH) -> ValidationConfig:
    return ValidationConfig.model_validate(yaml.safe_load(path.read_text()))


def expand_grid(grid: dict[str, Any], sleeve_name: str) -> list[dict[str, Any]]:
    """Cartesian product of the grid's value lists. A value may be a
    ``{sleeve: [...]}`` mapping for sleeve-specific choices."""
    keys: list[str] = []
    values: list[list[Any]] = []
    for key, choices in grid.items():
        if isinstance(choices, dict):
            choices = choices[sleeve_name]
        keys.append(key)
        values.append(list(choices))
    if not keys:
        return [{}]
    return [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*values)]


def _as_tuple(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_as_tuple(v) for v in value)
    return value


@dataclass(frozen=True)
class GridPoint:
    overrides: dict[str, Any]
    params: BaseModel
    params_hash: str
    result: vectorized.VectorizedResult


def _sharpe(r: pd.Series, ppy: float) -> float:
    return stats.sharpe_ratio(r, ppy)


def _summary(r: pd.Series, gross: pd.Series, turnover: pd.Series, ppy: float) -> dict[str, float]:
    curve = (1.0 + r).cumprod()
    curve0 = pd.concat([pd.Series([1.0], index=[r.index[0] - pd.Timedelta(seconds=1)]), curve])
    dd = stats.max_drawdown(curve0)
    years = len(r) / ppy
    return {
        "cagr": stats.cagr(curve0, ppy),
        "annualized_vol": stats.annualized_vol(r, ppy),
        "sharpe": stats.sharpe_ratio(r, ppy),
        "sortino": stats.sortino_ratio(r, ppy),
        "calmar": stats.calmar_ratio(stats.cagr(curve0, ppy), dd.max_drawdown),
        "max_drawdown": dd.max_drawdown,
        "max_drawdown_duration_bars": dd.duration_bars,
        "turnover_per_bar": float(turnover.mean()),
        "turnover_annual": float(turnover.mean() * ppy),
        "gross_cagr": stats.cagr(
            pd.concat([pd.Series([1.0], index=curve0.index[:1]), (1.0 + gross).cumprod()]), ppy
        ),
        "cost_drag_annual": stats.cost_drag(gross, r, ppy),
        "break_even_cost": stats.break_even_cost(gross, float(turnover.mean()), ppy),
        "hit_rate_bars": float((r[r != 0] > 0).mean()) if (r != 0).any() else float("nan"),
        "exposure": float("nan"),
        "years": years,
    }


def _buy_and_hold_returns(
    close: pd.DataFrame,
    weights0: dict[str, float],
    cost_models: dict[str, Any],
    capital: float,
) -> tuple[pd.Series, pd.Series]:
    """Exact buy-and-hold (fixed quantities, drifting weights, one entry
    cost), sidestepping the vectorized backtester's documented weight-
    drift-as-turnover limitation. Entry at the first bar's close."""
    rel = close / close.iloc[0]
    w0 = pd.Series({c: weights0.get(c, 0.0) for c in close.columns})
    value = (rel * w0).sum(axis=1)
    gross = value.pct_change().fillna(0.0)
    entry_cost = 0.0
    for c, w in weights0.items():
        if w <= 0:
            continue
        cm = cost_models[c]
        notional = w * capital
        entry_cost += max(notional * float(cm.fee_pct), float(cm.min_fee)) + float(cm.fixed_fee)
        entry_cost += min(notional * float(cm.tob_pct), float(cm.tob_cap or np.inf))
        entry_cost += notional * float(cm.spread_bps) / 20_000.0
    net = gross.copy()
    net.iloc[0] = -entry_cost / capital
    return net, gross


def _windows(
    index: pd.DatetimeIndex, start: pd.Timestamp, end: pd.Timestamp, train_days: int, test_days: int
) -> list[tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    """(train_start, test_start, test_end) rolling by test length; end exclusive."""
    out = []
    train_start = start
    while True:
        test_start = train_start + pd.Timedelta(days=train_days)
        test_end = min(test_start + pd.Timedelta(days=test_days), end)
        if test_start >= end or (index[(index >= test_start) & (index < test_end)].size < 2):
            break
        out.append((train_start, test_start, test_end))
        if test_end >= end:
            break
        train_start = train_start + pd.Timedelta(days=test_days)
    return out


def _heatmap(points: list[GridPoint], grid_keys: list[str], sharpes: list[float]) -> dict[str, Any]:
    if not grid_keys:
        return {"x_key": None, "y_key": None, "cells": []}
    x_key = grid_keys[0]
    y_key = grid_keys[1] if len(grid_keys) > 1 else None
    cells = []
    for p, s in zip(points, sharpes, strict=True):
        cells.append(
            {
                "x": str(p.overrides.get(x_key)),
                "y": str(p.overrides.get(y_key)) if y_key else "",
                "sharpe": s,
                "params": {k: str(v) for k, v in p.overrides.items()},
            }
        )
    return {"x_key": x_key, "y_key": y_key, "cells": cells}


def _sharp_peak(
    points: list[GridPoint], grid_keys: list[str], sharpes: list[float], threshold: float
) -> dict[str, Any]:
    """Is the best grid point an isolated spike? Neighbours = points that
    differ from it in exactly one parameter, by one step along that
    parameter's value list. A robust edge has neighbours nearly as good;
    a lucky one sits alone on a peak."""
    finite = [(i, s) for i, s in enumerate(sharpes) if s == s]
    if len(finite) < 2 or not grid_keys:
        return {"flag": False, "reason": "grid too small to judge"}
    best_i = max(finite, key=lambda t: t[1])[0]
    best = points[best_i].overrides
    orders = {k: list(dict.fromkeys(str(p.overrides[k]) for p in points)) for k in grid_keys}
    neighbours = []
    for i, p in enumerate(points):
        diffs = [k for k in grid_keys if str(p.overrides[k]) != str(best[k])]
        if len(diffs) != 1 or sharpes[i] != sharpes[i]:  # not a neighbour, or NaN Sharpe
            continue
        k = diffs[0]
        if abs(orders[k].index(str(p.overrides[k])) - orders[k].index(str(best[k]))) == 1:
            neighbours.append(sharpes[i])
    if not neighbours:
        return {"flag": False, "reason": "no neighbours"}
    gap = sharpes[best_i] - float(np.median(neighbours))
    return {
        "flag": bool(gap > threshold),
        "best_sharpe": sharpes[best_i],
        "neighbour_median_sharpe": float(np.median(neighbours)),
        "gap": gap,
        "threshold": threshold,
    }


def run_validation(
    strategy_name: str,
    sleeve: SleeveConfig,
    price_set: inputs.PriceSet,
    vconf: ValidationConfig,
    ledger: Ledger,
    run_id: str,
    git_sha: str | None,
    evaluate_holdout: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    sv = vconf.strategies[strategy_name]
    windows_conf = vconf.sleeves[sleeve.name]
    bpd = sleeve.bars_per_day
    ppy = sleeve.periods_per_year
    capital = float(sleeve.paper_capital)
    limits = sleeve.risk.to_risk_limits()
    costs = inputs.cost_models(sleeve)

    close_full = price_set.close_panel
    n = len(close_full)
    hs = math.floor(n * (1.0 - vconf.holdout_fraction))
    holdout_start = close_full.index[hs]
    pre = close_full.iloc[:hs]
    vol_pre = vectorized.trailing_vol(pre, max(2, round(35 * bpd)))
    data_hash = hash_dataframe(pre)

    # --- step 2: every grid point over the whole pre-hold-out period ---
    grid = expand_grid(sv.grid, sleeve.name)
    grid_keys = list(sv.grid.keys())
    points: list[GridPoint] = []
    for overrides in grid:
        typed = {k: _as_tuple(v) for k, v in overrides.items()}
        params = inputs.strategy_params(strategy_name, sleeve, typed)
        w = inputs.vectorized_weights(strategy_name, params, pre)
        w = constrain_weight_frame(w.fillna(0.0), limits)
        result = vectorized.run_with_costs(pre, w, costs, capital, sleeve.buffer_pct, vol_pre)
        points.append(
            GridPoint(overrides, params, hash_config(params.model_dump(mode="json")), result)
        )

    warmup_bars = round(sv.warmup_days * bpd)
    eval_start = pre.index[min(warmup_bars, len(pre) - 1)]
    eval_mask = pre.index >= eval_start

    grid_sharpes = [_sharpe(p.result.net_returns[eval_mask], ppy) for p in points]
    config_hash = hash_config(
        {"strategy": strategy_name, "sleeve": sleeve.model_dump(mode="json"), "grid": sv.grid}
    )
    ledger.record_trials(
        [
            Trial(
                run_id=run_id,
                strategy=strategy_name,
                sleeve=sleeve.name,
                kind="grid",
                params=p.params.model_dump(mode="json"),
                params_hash=p.params_hash,
                config_hash=config_hash,
                data_hash=data_hash,
                git_sha=git_sha,
                period_start=eval_start.to_pydatetime(),
                period_end=pre.index[-1].to_pydatetime(),
                n_bars=int(eval_mask.sum()),
                sharpe_per_period=overfitting.per_period_sharpe(p.result.net_returns[eval_mask]),
                sharpe_annual=s,
                metrics=tables.clean(
                    {
                        "cagr": stats.cagr(p.result.equity_curve[eval_mask], ppy),
                        "turnover_per_bar": float(p.result.turnover_per_bar[eval_mask].mean()),
                    }
                ),  # type: ignore[arg-type]
            )
            for p, s in zip(points, grid_sharpes, strict=True)
        ]
    )

    # --- step 3: walk-forward ---
    wins = _windows(
        pd.DatetimeIndex(pre.index),
        eval_start,
        pre.index[-1] + pd.Timedelta(seconds=1),
        windows_conf.train_days,
        windows_conf.test_days,
    )
    if not wins:
        raise ValueError(
            f"not enough pre-hold-out history for even one {windows_conf.train_days}d train + "
            f"{windows_conf.test_days}d test window after {sv.warmup_days}d warm-up"
        )
    chosen: list[int] = []
    segments: list[pd.Series] = []
    gross_segments: list[pd.Series] = []
    turnover_segments: list[pd.Series] = []
    window_rows = []
    idx = pre.index
    for train_start, test_start, test_end in wins:
        train_mask = (idx >= train_start) & (idx < test_start)
        test_mask = (idx >= test_start) & (idx < test_end)
        train_sharpes = [_sharpe(p.result.net_returns[train_mask], ppy) for p in points]
        finite = [(i, s) for i, s in enumerate(train_sharpes) if s == s]
        best = max(finite, key=lambda t: t[1])[0] if finite else 0
        chosen.append(best)
        seg = points[best].result.net_returns[test_mask]
        segments.append(seg)
        gross_segments.append(points[best].result.gross_returns[test_mask])
        turnover_segments.append(points[best].result.turnover_per_bar[test_mask])
        window_rows.append(
            {
                "train_start": train_start,
                "test_start": test_start,
                "test_end": test_end,
                "chosen_params": {k: str(v) for k, v in points[best].overrides.items()},
                "train_sharpe": train_sharpes[best],
                "test_sharpe": _sharpe(seg, ppy),
                "test_return": float(np.prod(1.0 + seg.to_numpy()) - 1.0),
            }
        )
    oos = pd.concat(segments)
    oos_gross = pd.concat(gross_segments)
    oos_turnover = pd.concat(turnover_segments)
    oos_summary = _summary(oos, oos_gross, oos_turnover, ppy)
    oos_start, oos_end = oos.index[0], oos.index[-1]

    # Baseline over exactly the same OOS period, same costs.
    baseline_name = sv.baseline
    oos_close = pre[(pre.index >= oos_start) & (pre.index <= oos_end)]
    baseline_oos = _baseline_returns(
        baseline_name, strategy_name, points[0].params, sleeve, oos_close, costs, capital, limits
    )

    # --- step 4: DSR from the ledger's trial history ---
    trial_sharpes = ledger.trial_sharpes(strategy_name, sleeve.name)
    n_trials = max(len(trial_sharpes), len(points))
    sr_var = float(np.var(list(trial_sharpes.values()), ddof=1)) if len(trial_sharpes) > 1 else 0.0
    dsr = overfitting.deflated_sharpe_ratio(oos.to_numpy(), n_trials, sr_var)
    psr0 = overfitting.probabilistic_sharpe_ratio(oos.to_numpy(), 0.0)

    # --- step 5: PBO ---
    trial_matrix = pd.DataFrame({i: p.result.net_returns[eval_mask] for i, p in enumerate(points)})
    pbo = overfitting.probability_of_backtest_overfitting(trial_matrix, vconf.pbo_blocks)

    # --- step 6: the rest ---
    block = max(2, round(vconf.bootstrap_block_days * bpd))
    boot = overfitting.block_bootstrap_sharpe_ci(oos, ppy, block, vconf.bootstrap_resamples)

    cost_sensitivity = []
    for m in vconf.cost_multipliers:
        cache: dict[int, pd.Series] = {}
        segs = []
        for (_, test_start, test_end), best in zip(wins, chosen, strict=True):
            if best not in cache:
                if m == 1.0:
                    cache[best] = points[best].result.net_returns
                else:
                    w = points[best].result.raw_weights
                    cache[best] = vectorized.run_with_costs(
                        pre, w, costs, capital, sleeve.buffer_pct, vol_pre, cost_multiplier=m
                    ).net_returns
            mask = (idx >= test_start) & (idx < test_end)
            segs.append(cache[best][mask])
        r = pd.concat(segs)
        cost_sensitivity.append(
            {
                "multiplier": m,
                "sharpe": _sharpe(r, ppy),
                "cagr": stats.cagr(
                    pd.concat([pd.Series([1.0]), (1 + r).cumprod().reset_index(drop=True)]), ppy
                ),
            }
        )

    benchmark = pre[sleeve.regime_benchmark]
    labels = tables.regime_labels(
        benchmark,
        round(vconf.regime_sma_days * bpd),
        round(vconf.regime_slope_days * bpd),
    )
    regime_table = tables.per_regime(oos, labels, ppy)
    year_table = tables.per_year(oos, ppy)

    heatmap = _heatmap(points, grid_keys, grid_sharpes)
    peak = _sharp_peak(points, grid_keys, grid_sharpes, vconf.sharp_peak_threshold)

    finite_full = [(i, s) for i, s in enumerate(grid_sharpes) if s == s]
    final_i = max(finite_full, key=lambda t: t[1])[0] if finite_full else 0
    final = points[final_i]
    unbuffered = vectorized.run_with_costs(
        pre, final.result.raw_weights, costs, capital, 0.0, vol_pre
    )
    buffering = {
        "buffer_pct": sleeve.buffer_pct,
        "turnover_annual_unbuffered": float(unbuffered.turnover_per_bar[eval_mask].mean() * ppy),
        "turnover_annual_buffered": float(final.result.turnover_per_bar[eval_mask].mean() * ppy),
        "sharpe_unbuffered": _sharpe(unbuffered.net_returns[eval_mask], ppy),
        "sharpe_buffered": grid_sharpes[final_i],
    }

    # --- step 7: hold-out ---
    prior_accesses = ledger.holdout_access_count(
        strategy_name, sleeve.name, holdout_start.to_pydatetime()
    )
    holdout: dict[str, Any] = {
        "evaluated": False,
        "start": holdout_start,
        "end": close_full.index[-1],
        "n_bars": n - hs,
        "prior_accesses": prior_accesses,
        "contaminated": prior_accesses >= 2,
    }
    holdout_curves: dict[str, Any] = {}
    if evaluate_holdout:
        prior = ledger.record_holdout_access(
            strategy_name, sleeve.name, holdout_start.to_pydatetime(), run_id, git_sha
        )
        vol_full = vectorized.trailing_vol(close_full, max(2, round(35 * bpd)))
        w_full = constrain_weight_frame(
            inputs.vectorized_weights(strategy_name, final.params, close_full).fillna(0.0), limits
        )
        full = vectorized.run_with_costs(
            close_full, w_full, costs, capital, sleeve.buffer_pct, vol_full
        )
        ho_mask = close_full.index >= holdout_start
        ho = full.net_returns[ho_mask]
        ho_summary = _summary(ho, full.gross_returns[ho_mask], full.turnover_per_bar[ho_mask], ppy)
        ho_base = _baseline_returns(
            baseline_name,
            strategy_name,
            final.params,
            sleeve,
            close_full[ho_mask],
            costs,
            capital,
            limits,
        )
        holdout.update(
            {
                "evaluated": True,
                "prior_accesses": prior,
                "contaminated": prior >= 1,
                "params": {k: str(v) for k, v in final.overrides.items()},
                "summary": ho_summary,
                "baseline_summary": _summary(
                    ho_base, ho_base, pd.Series(0.0, index=ho_base.index), ppy
                ),
                "dsr": overfitting.deflated_sharpe_ratio(ho.to_numpy(), n_trials, sr_var).dsr,
            }
        )
        holdout_curves = {
            "strategy": _norm_curve(ho),
            "baseline": _norm_curve(ho_base),
        }
        ledger.record_trials(
            [
                Trial(
                    run_id=run_id,
                    strategy=strategy_name,
                    sleeve=sleeve.name,
                    kind="holdout",
                    params=final.params.model_dump(mode="json"),
                    params_hash=final.params_hash,
                    config_hash=config_hash,
                    data_hash=hash_dataframe(close_full),
                    git_sha=git_sha,
                    period_start=holdout_start.to_pydatetime(),
                    period_end=close_full.index[-1].to_pydatetime(),
                    n_bars=int(ho_mask.sum()),
                    sharpe_per_period=overfitting.per_period_sharpe(ho),
                    sharpe_annual=ho_summary["sharpe"],
                    metrics=tables.clean(ho_summary),  # type: ignore[arg-type]
                )
            ]
        )

    oos_equity = (1 + oos).cumprod()
    report: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "strategy": strategy_name,
        "sleeve": sleeve.name,
        "generated_at": now or pd.Timestamp.now(tz="UTC"),
        "git_sha": git_sha,
        "config_hash": config_hash,
        "data_hash": data_hash,
        "periods_per_year": ppy,
        "data": {
            "start": close_full.index[0],
            "end": close_full.index[-1],
            "n_bars": n,
            "assets": sleeve.asset_ids,
            "research_listings": {a.id: a.research.instrument_id for a in sleeve.assets},
            "traded_listings": {a.id: a.live.instrument_id for a in sleeve.assets},
            "dropped_for_alignment": price_set.dropped_bars,
        },
        "headline": {
            "dsr": dsr.dsr,
            "oos_sharpe": oos_summary["sharpe"],
            "oos_cagr": oos_summary["cagr"],
            "oos_max_drawdown": oos_summary["max_drawdown"],
            "baseline": baseline_name,
            "baseline_oos_sharpe": _sharpe(baseline_oos, ppy),
            "pbo": pbo.pbo,
            "psr_vs_zero": psr0,
            "sharpe_ci": [boot.lower, boot.upper],
            "n_trials": n_trials,
            "sharp_peak": peak["flag"],
        },
        "dsr": {
            "dsr": dsr.dsr,
            "observed_sr_per_period": dsr.observed_sr,
            "hurdle_sr_per_period": dsr.hurdle_sr,
            "hurdle_sr_annual": dsr.hurdle_sr * math.sqrt(ppy),
            "n_trials": n_trials,
            "trial_sharpe_variance": sr_var,
        },
        "pbo": {
            "pbo": pbo.pbo,
            "n_splits": pbo.n_splits,
            "n_trials": pbo.n_trials,
            "n_blocks": vconf.pbo_blocks,
        },
        "bootstrap": {
            "sharpe": boot.sharpe,
            "lower": boot.lower,
            "upper": boot.upper,
            "confidence": boot.confidence,
            "block_bars": boot.block_length,
            "resamples": boot.n_resamples,
        },
        "walk_forward": {
            "train_days": windows_conf.train_days,
            "test_days": windows_conf.test_days,
            "warmup_days": sv.warmup_days,
            "eval_start": eval_start,
            "oos_start": oos_start,
            "oos_end": oos_end,
            "windows": window_rows,
        },
        "oos": oos_summary,
        "baseline_oos": _summary(
            baseline_oos, baseline_oos, pd.Series(0.0, index=baseline_oos.index), ppy
        ),
        "in_sample_best": {
            "params": {k: str(v) for k, v in final.overrides.items()},
            "sharpe": grid_sharpes[final_i],
            "note": "Best grid point over the WHOLE pre-hold-out period — chosen with hindsight; for comparison only.",
        },
        "cost_sensitivity": cost_sensitivity,
        "per_year": year_table,
        "per_regime": regime_table,
        "heatmap": heatmap,
        "sharp_peak": peak,
        "buffering": buffering,
        "holdout": holdout,
        "cost_assumptions": {
            a.id: {k: str(v) for k, v in a.costs.model_dump().items()} for a in sleeve.assets
        },
        "curves": {
            "oos": _norm_curve(oos),
            "baseline": _norm_curve(baseline_oos),
            "oos_drawdown": _downsample(tables.drawdown_series(oos_equity)),
            "holdout": holdout_curves,
        },
        "verdict": _verdict(
            dsr.dsr, pbo.pbo, oos_summary["sharpe"], _sharpe(baseline_oos, ppy), peak["flag"]
        ),
    }
    report = tables.clean(report)  # type: ignore[assignment]
    ledger.save_report(run_id, strategy_name, sleeve.name, report)
    return report


def _baseline_returns(
    baseline_name: str,
    strategy_name: str,
    strategy_params: BaseModel,
    sleeve: SleeveConfig,
    close: pd.DataFrame,
    costs: dict[str, Any],
    capital: float,
    limits: Any,
) -> pd.Series:
    symbol = getattr(strategy_params, "symbol", None)
    if baseline_name == "buy_and_hold":
        # A single-instrument strategy is compared with holding that one instrument.
        w0 = {symbol: 1.0} if symbol else {c: 1.0 / len(close.columns) for c in close.columns}
        net, _ = _buy_and_hold_returns(close, w0, costs, capital)
        return net
    params = inputs.strategy_params(baseline_name, sleeve)
    w = constrain_weight_frame(inputs.vectorized_weights(baseline_name, params, close), limits)
    vol = vectorized.trailing_vol(close, max(2, round(35 * sleeve.bars_per_day)))
    return vectorized.run_with_costs(close, w, costs, capital, sleeve.buffer_pct, vol).net_returns


def _downsample(series: pd.Series, max_points: int = 600) -> list[list[Any]]:
    if len(series) > max_points:
        step = math.ceil(len(series) / max_points)
        series = pd.concat([series.iloc[::step], series.iloc[-1:]])
        series = series[~series.index.duplicated(keep="last")]
    return [[pd.Timestamp(ts).isoformat(), float(v)] for ts, v in series.items()]  # type: ignore[arg-type]


def _norm_curve(returns: pd.Series) -> list[list[Any]]:
    """Equity indexed to 100 at the start (the public API never shows
    absolute balances — spec Section 8.1)."""
    return _downsample(100.0 * (1.0 + returns).cumprod())


def _verdict(
    dsr: float, pbo: float, oos_sharpe: float, base_sharpe: float, sharp_peak: bool
) -> dict[str, Any]:
    """Plain-language pass/fail. Deliberately strict; 'survives' needs all four."""
    checks = {
        "dsr_above_0.95": bool(dsr == dsr and dsr > 0.95),
        "pbo_below_0.5": bool(
            pbo != pbo or pbo < 0.5
        ),  # NaN = single-config strategy, not overfit by selection
        "beats_baseline_oos": bool(
            oos_sharpe == oos_sharpe and base_sharpe == base_sharpe and oos_sharpe > base_sharpe
        ),
        "no_sharp_peak": not sharp_peak,
    }
    return {"survives": all(checks.values()), "checks": checks}
