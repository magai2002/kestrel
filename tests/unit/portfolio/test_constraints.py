from __future__ import annotations

import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from portfolio.constraints import constrain_weight_frame, constrain_weights
from risk.limits import GroupCap, RiskLimits
from risk.pre_trade import check_gross_exposure, check_group_cap, check_weight_cap

LIMITS = RiskLimits(
    per_instrument_weight_cap=0.30,
    group_caps=(GroupCap(name="eq", instrument_ids=("A", "B", "C"), cap=0.60),),
    gross_exposure_cap=1.0,
)


def test_clips_each_weight_to_cap_and_floors_at_zero() -> None:
    out = constrain_weights({"A": 0.5, "D": -0.2}, LIMITS)
    assert out == {"A": 0.30, "D": 0.0}


def test_scales_group_down_proportionally() -> None:
    out = constrain_weights({"A": 0.3, "B": 0.3, "C": 0.3, "D": 0.1}, LIMITS)
    assert sum(out[k] for k in "ABC") == pytest.approx(0.60)
    assert out["A"] == pytest.approx(out["B"])
    assert out["D"] == 0.1  # outside the group: untouched


def test_scales_gross_down_last() -> None:
    out = constrain_weights({k: 0.3 for k in "DEFG"}, LIMITS)
    assert sum(out.values()) == pytest.approx(1.0)
    assert sum(out.values()) <= 1.0


def test_within_limits_is_unchanged() -> None:
    w = {"A": 0.1, "D": 0.2}
    assert constrain_weights(w, LIMITS) == w


def test_frame_version_matches_dict_version_row_by_row() -> None:
    rows = [
        {"A": 0.5, "B": 0.4, "C": 0.3, "D": 0.6},
        {"A": 0.1, "B": 0.0, "C": 0.0, "D": 0.1},
        {"A": 0.3, "B": 0.3, "C": 0.3, "D": 0.3},
    ]
    frame = constrain_weight_frame(pd.DataFrame(rows), LIMITS)
    for i, row in enumerate(rows):
        expected = constrain_weights(row, LIMITS)
        for k, v in expected.items():
            assert frame.iloc[i][k] == pytest.approx(v)


@given(
    st.dictionaries(
        st.sampled_from(list("ABCDEFG")),
        st.floats(min_value=-3, max_value=3, allow_nan=False),
        min_size=1,
        max_size=7,
    )
)
@settings(max_examples=300)
def test_constrained_weights_always_pass_pre_trade_checks(weights: dict[str, float]) -> None:
    """Section 11 property, extended: whatever a strategy asks for, the
    portfolio layer's output never trips the weight/group/gross checks —
    so risk only ever rejects genuine anomalies."""
    out = constrain_weights(weights, LIMITS)
    assert check_gross_exposure(out, LIMITS).approved
    for k, v in out.items():
        assert v >= 0
        assert check_weight_cap(v, LIMITS).approved
        assert check_group_cap(k, out, LIMITS).approved


def test_plan_rebalance_trades_down_a_drifted_position_even_inside_the_buffer() -> None:
    """Regression (Phase 6, real 10-ETF backtest): an untraded asset that
    drifted above its cap used to stay there, and the group/gross check
    then rejected every order in the group."""
    from datetime import UTC, datetime
    from decimal import Decimal

    from portfolio.rebalance import plan_rebalance

    limits = RiskLimits(
        per_instrument_weight_cap=0.30,
        group_caps=(GroupCap(name="eq", instrument_ids=("A", "B"), cap=0.60),),
    )
    # A has drifted to 34% (cap 30%); target 30% is inside the 10-point buffer.
    plan = plan_rebalance(
        raw_target_weights={"A": 0.30, "B": 0.30},
        held_quantities={"A": Decimal(34), "B": Decimal(20)},
        last_prices={"A": Decimal(100), "B": Decimal(100)},
        sleeve_equity=Decimal(10_000),
        symbols={"A": "A", "B": "B"},
        step_sizes={"A": Decimal(1), "B": Decimal(1)},
        min_notionals={"A": Decimal(0), "B": Decimal(0)},
        risk_limits=limits,
        buffer_pct=0.10,
        sleeve="s",
        strategy="t",
        bar_close_ts=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert plan.buffered_weights["A"] <= 0.30
    assert any(o.instrument_id == "A" and o.side.value == "sell" for o in plan.approved)
    assert plan.rejected == []
