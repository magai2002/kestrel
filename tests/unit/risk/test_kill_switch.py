from datetime import UTC, datetime

from risk.kill_switch import KillSwitchState, check_daily_loss, check_drawdown, evaluate


def test_check_daily_loss_within_limit_is_none() -> None:
    assert check_daily_loss(10000, 9800, 0.03) is None  # -2%, within 3%


def test_check_daily_loss_breach_hand_computed() -> None:
    reason = check_daily_loss(10000, 9600, 0.03)  # -4%, breaches 3%
    assert reason is not None
    assert "4.0%" in reason or "-4.0%" in reason


def test_check_drawdown_within_limit_is_none() -> None:
    assert check_drawdown(10000, 8500, 0.20) is None  # -15%, within 20%


def test_check_drawdown_breach() -> None:
    assert check_drawdown(10000, 7000, 0.20) is not None  # -30%, breaches 20%


def test_evaluate_trips_on_drawdown() -> None:
    now = datetime(2024, 1, 1, tzinfo=UTC)
    state = evaluate(
        KillSwitchState(),
        equity_today_open=9000,
        equity_peak=10000,
        equity_now=7000,
        now=now,
        daily_loss_limit_pct=0.03,
        drawdown_limit_pct=0.20,
    )
    assert state.tripped
    assert state.tripped_at == now


def test_evaluate_stays_untripped_within_all_limits() -> None:
    state = evaluate(
        KillSwitchState(),
        equity_today_open=10000,
        equity_peak=10000,
        equity_now=9900,
        now=datetime(2024, 1, 1, tzinfo=UTC),
        daily_loss_limit_pct=0.03,
        drawdown_limit_pct=0.20,
    )
    assert not state.tripped


def test_evaluate_never_untrips_even_if_equity_recovers() -> None:
    tripped_at = datetime(2024, 1, 1, tzinfo=UTC)
    state = KillSwitchState(tripped=True, reason="previously tripped", tripped_at=tripped_at)
    result = evaluate(
        state,
        equity_today_open=10000,
        equity_peak=10000,
        equity_now=10000,  # fully recovered
        now=datetime(2024, 1, 2, tzinfo=UTC),
        daily_loss_limit_pct=0.03,
        drawdown_limit_pct=0.20,
    )
    assert result.tripped
    assert result.tripped_at == tripped_at  # unchanged -- still the original trip
