import pandas as pd
import pytest

from portfolio.sizing import buffered_target, should_trade, vol_target_weight


def test_vol_target_weight_scalar_hand_computed() -> None:
    assert vol_target_weight(0.15, 0.30) == pytest.approx(0.5)


def test_vol_target_weight_scalar_zero_on_zero_vol() -> None:
    assert vol_target_weight(0.15, 0.0) == 0.0


def test_vol_target_weight_scalar_zero_on_negative_vol() -> None:
    assert vol_target_weight(0.15, -0.1) == 0.0


def test_vol_target_weight_scalar_zero_on_nan() -> None:
    assert vol_target_weight(0.15, float("nan")) == 0.0


def test_vol_target_weight_series_matches_scalar_elementwise() -> None:
    realized = pd.Series([0.30, 0.0, -0.1, float("nan"), 0.15])
    result = vol_target_weight(0.15, realized)
    assert list(result) == [pytest.approx(0.5), 0.0, 0.0, 0.0, pytest.approx(1.0)]


def test_should_trade_within_buffer_is_false() -> None:
    assert should_trade(current_weight=0.50, target_weight=0.55, buffer_pct=0.10) is False


def test_should_trade_outside_buffer_is_true() -> None:
    assert should_trade(current_weight=0.50, target_weight=0.65, buffer_pct=0.10) is True


def test_should_trade_zero_buffer_means_any_difference_trades() -> None:
    assert should_trade(current_weight=0.50, target_weight=0.50001, buffer_pct=0.0) is True
    assert should_trade(current_weight=0.50, target_weight=0.50, buffer_pct=0.0) is False


def test_buffered_target_holds_within_buffer() -> None:
    assert buffered_target(current_weight=0.50, target_weight=0.55, buffer_pct=0.10) == 0.50


def test_buffered_target_moves_outside_buffer() -> None:
    assert buffered_target(current_weight=0.50, target_weight=0.65, buffer_pct=0.10) == 0.65
