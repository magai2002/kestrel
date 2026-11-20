"""End-to-end validation pipeline on synthetic data, with an in-memory ledger."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.inputs import PriceSet
from config.sleeve import SleeveConfig
from research import report_html, validation
from research.ledger import InMemoryLedger

N_BARS = 1600


def _sleeve() -> SleeveConfig:
    listing = {"venue": "binance", "base_currency": "X", "quote_currency": "USDT"}
    return SleeveConfig.model_validate(
        {
            "name": "crypto",
            "asset_class": "crypto",
            "decision_bar": "1d",
            "base_currency": "USD",
            "paper_capital": 10_000,
            "periods_per_year_days": 365,
            "regime_benchmark": "A",
            "assets": [
                {
                    "id": aid,
                    "research": {**listing, "symbol": aid},
                    "live": {**listing, "symbol": aid},
                    "costs": {"fee_pct": "0.001", "spread_bps": "2"},
                }
                for aid in ("A", "B")
            ],
            "risk": {"per_instrument_weight_cap": 0.6},
        }
    )


def _vconf() -> validation.ValidationConfig:
    return validation.ValidationConfig.model_validate(
        {
            "holdout_fraction": 0.25,
            "bootstrap_block_days": 10,
            "bootstrap_resamples": 200,
            "pbo_blocks": 8,
            "regime_sma_days": 50,
            "regime_slope_days": 10,
            "cost_multipliers": [0, 1, 2],
            "sharp_peak_threshold": 0.3,
            "sleeves": {"crypto": {"train_days": 300, "test_days": 100}},
            "strategies": {
                "trend_vol_target": {
                    "baseline": "buy_and_hold",
                    "warmup_days": 60,
                    "grid": {
                        "speeds_days": [[[4, 16], [8, 32]], [[8, 32], [16, 64]]],
                        "vol_span_days": [10, 20],
                    },
                },
                "buy_and_hold": {
                    "baseline": "equal_weight_rebalanced",
                    "warmup_days": 0,
                    "grid": {},
                },
            },
        }
    )


def _prices(seed: int = 0, holdout_shock: float = 0.0) -> PriceSet:
    rng = np.random.default_rng(seed)
    open_time = pd.date_range("2019-01-01", periods=N_BARS, freq="1D", tz="UTC")
    bars = {}
    for j, aid in enumerate(("A", "B")):
        r = rng.normal(0.0004 * (j + 1), 0.02, N_BARS)
        r[int(N_BARS * 0.75) :] += holdout_shock
        close = 100 * np.exp(np.cumsum(r))
        bars[aid] = pd.DataFrame(
            {
                "open_time": open_time,
                "close_time": open_time + pd.Timedelta(days=1),
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 1e6,
            }
        )
    return PriceSet(bars, {"A": 0, "B": 0})


def test_report_has_every_section_and_leads_with_dsr() -> None:
    ledger = InMemoryLedger()
    report = validation.run_validation(
        "trend_vol_target", _sleeve(), _prices(), _vconf(), ledger, "run-1", None
    )
    for key in (
        "headline",
        "dsr",
        "pbo",
        "bootstrap",
        "walk_forward",
        "oos",
        "baseline_oos",
        "cost_sensitivity",
        "per_year",
        "per_regime",
        "heatmap",
        "buffering",
        "holdout",
    ):
        assert key in report
    assert next(iter(report["headline"])) == "dsr"
    assert report["headline"]["n_trials"] == 4
    assert len(report["cost_sensitivity"]) == 3
    assert len(ledger.trials) == 4  # one grid trial per param set
    html = report_html.render(report)
    assert html.index("Deflated Sharpe") < html.index("Parameter stability")


def test_holdout_untouched_unless_asked() -> None:
    ledger = InMemoryLedger()
    report = validation.run_validation(
        "trend_vol_target", _sleeve(), _prices(), _vconf(), ledger, "run-1", None
    )
    assert ledger.accesses == []
    assert report["holdout"]["evaluated"] is False
    oos_end = pd.Timestamp(report["walk_forward"]["oos_end"])
    assert oos_end < pd.Timestamp(report["holdout"]["start"])


def test_out_of_sample_results_cannot_see_the_holdout() -> None:
    """Change every hold-out price; nothing about the walk-forward may move."""
    a = validation.run_validation(
        "trend_vol_target", _sleeve(), _prices(), _vconf(), InMemoryLedger(), "a", None
    )
    b = validation.run_validation(
        "trend_vol_target",
        _sleeve(),
        _prices(holdout_shock=0.05),
        _vconf(),
        InMemoryLedger(),
        "b",
        None,
    )
    assert a["oos"] == b["oos"]
    assert a["walk_forward"]["windows"] == b["walk_forward"]["windows"]
    assert a["data_hash"] == b["data_hash"]


def test_second_holdout_access_marks_report_contaminated() -> None:
    ledger = InMemoryLedger()
    args = ("trend_vol_target", _sleeve(), _prices(), _vconf(), ledger)
    first = validation.run_validation(*args, "run-1", None, evaluate_holdout=True)
    assert first["holdout"]["evaluated"] and not first["holdout"]["contaminated"]
    second = validation.run_validation(*args, "run-2", None, evaluate_holdout=True)
    assert second["holdout"]["contaminated"]
    assert len(ledger.accesses) == 2
    # Even a run that doesn't look again reports the contamination.
    third = validation.run_validation(*args, "run-3", None)
    assert third["holdout"]["contaminated"]


def test_trial_count_accumulates_across_runs_but_not_for_repeats() -> None:
    ledger = InMemoryLedger()
    validation.run_validation(
        "trend_vol_target", _sleeve(), _prices(), _vconf(), ledger, "r1", None
    )
    again = validation.run_validation(
        "trend_vol_target", _sleeve(), _prices(), _vconf(), ledger, "r2", None
    )
    assert again["headline"]["n_trials"] == 4


def test_single_config_strategy_runs_without_grid() -> None:
    report = validation.run_validation(
        "buy_and_hold", _sleeve(), _prices(), _vconf(), InMemoryLedger(), "bh", None
    )
    assert report["headline"]["n_trials"] == 1
    assert report["pbo"]["pbo"] is None  # NaN -> None in JSON
    assert report["dsr"]["hurdle_sr_per_period"] == pytest.approx(0.0)


def test_expand_grid_handles_sleeve_specific_values() -> None:
    grid = {"lookback_months": [6, 12], "top_n": {"crypto": [1], "equities": [2, 3]}}
    assert len(validation.expand_grid(grid, "crypto")) == 2
    assert len(validation.expand_grid(grid, "equities")) == 4
    assert validation.expand_grid({}, "crypto") == [{}]
