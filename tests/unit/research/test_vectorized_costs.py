"""``run_with_costs``: the cost-model-faithful vectorized backtester."""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from execution.costs import CostModel
from research import vectorized

IDX = pd.date_range("2024-01-01", periods=5, freq="1D", tz="UTC")


def _flat(cols: list[str]) -> pd.DataFrame:
    return pd.DataFrame(100.0, index=IDX, columns=cols)


def test_zero_cost_matches_plain_run() -> None:
    rng = np.random.default_rng(0)
    idx = pd.date_range("2024-01-01", periods=200, freq="1D", tz="UTC")
    prices = pd.DataFrame({"A": 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 200)))}, index=idx)
    weights = pd.DataFrame({"A": rng.uniform(0, 1, 200)}, index=idx)
    zero = {"A": CostModel(fee_pct=Decimal(0), spread_bps=Decimal(0), slippage_k=Decimal(0))}
    a = vectorized.run(prices, weights, buffer_pct=0.1, cost_bps=0)
    b = vectorized.run_with_costs(prices, weights, zero, 10_000, buffer_pct=0.1)
    pd.testing.assert_series_equal(a.net_returns, b.net_returns, check_names=False)


def test_minimum_commission_charged_on_small_trade() -> None:
    """Buy 5% of a $10,000 sleeve ($500): 0.05% would be $0.25, the
    minimum is $1.70, plus $0.21 fixed and 0.12% TOB ($0.60) = $2.51."""
    weights = pd.DataFrame({"A": [0.05] * 5}, index=IDX)
    cm = CostModel(
        fee_pct=Decimal("0.0005"),
        min_fee=Decimal("1.70"),
        fixed_fee=Decimal("0.21"),
        spread_bps=Decimal(0),
        slippage_k=Decimal(0),
        tob_pct=Decimal("0.0012"),
    )
    res = vectorized.run_with_costs(_flat(["A"]), weights, {"A": cm}, 10_000)
    # Trade happens when the shifted weight first becomes nonzero: bar 1.
    assert res.net_returns.iloc[1] == pytest.approx(-2.51 / 10_000)
    assert (res.net_returns.iloc[2:] == 0).all()
    assert res.turnover_per_bar.iloc[1] == pytest.approx(0.05)


def test_cost_multiplier_scales_costs() -> None:
    weights = pd.DataFrame({"A": [0.5] * 5}, index=IDX)
    cm = CostModel(fee_pct=Decimal("0.001"), spread_bps=Decimal(0), slippage_k=Decimal(0))
    one = vectorized.run_with_costs(_flat(["A"]), weights, {"A": cm}, 10_000)
    three = vectorized.run_with_costs(_flat(["A"]), weights, {"A": cm}, 10_000, cost_multiplier=3)
    zero = vectorized.run_with_costs(_flat(["A"]), weights, {"A": cm}, 10_000, cost_multiplier=0)
    assert three.net_returns.iloc[1] == pytest.approx(3 * one.net_returns.iloc[1])
    assert (zero.net_returns == 0).all()


def test_tob_cap_applies() -> None:
    weights = pd.DataFrame({"A": [1.0] * 5}, index=IDX)
    cm = CostModel(
        fee_pct=Decimal(0),
        spread_bps=Decimal(0),
        slippage_k=Decimal(0),
        tob_pct=Decimal("0.0132"),
        tob_cap=Decimal("50"),
    )
    res = vectorized.run_with_costs(_flat(["A"]), weights, {"A": cm}, 10_000)
    assert res.net_returns.iloc[1] == pytest.approx(-50 / 10_000)


def test_numpy_apply_buffer_matches_reference_semantics() -> None:
    """The numpy rewrite keeps the original pandas loop's behaviour."""
    rng = np.random.default_rng(9)
    w = pd.DataFrame(rng.uniform(0, 1, (300, 3)), columns=list("abc"))
    w.iloc[5, 1] = np.nan
    expected = w.copy()
    held = w.iloc[0].copy()
    for i in range(1, len(w)):
        target = w.iloc[i]
        trade = (target - held).abs() > 0.2
        held = held.where(~trade, target)
        expected.iloc[i] = held
    pd.testing.assert_frame_equal(vectorized.apply_buffer(w, 0.2), expected)
