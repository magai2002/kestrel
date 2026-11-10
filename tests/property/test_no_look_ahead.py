"""Spec Section 11, Property tests: "No look-ahead: changing any bar
after t never changes decisions at or before t."

``on_bar``'s no-look-ahead is structural — it only ever sees a
``StrategyContext`` that literally doesn't contain anything after
"now." ``compute_weights`` operates on a whole price panel at once, so
its only protection is that every internal pandas operation (rolling
windows, EWMA, resampling) is itself causal. That's exactly the kind of
thing a careful-looking implementation can still get wrong (a centered
rolling window, an off-by-one shift) — so this constructs two price
series sharing an identical prefix up to a cutoff and a *deliberately
very different* (higher-volatility, independently-seeded) tail, and
checks the weights before the cutoff are unaffected by the tail.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from strategies.trend_vol_target import TrendVolTargetParams
from strategies.trend_vol_target import compute_weights as trend_weights
from strategies.xs_momentum import XSMomentumParams
from strategies.xs_momentum import compute_weights as xs_momentum_weights

SHORT_TREND_PARAMS = TrendVolTargetParams(
    speeds_days=((2, 4), (3, 6)), vol_span_days=3, bars_per_day=1.0, periods_per_year=252.0
)
SHORT_XS_PARAMS = XSMomentumParams(lookback_months=1, top_n=2, vol_span_days=5, bars_per_day=1.0)


def _diverging_series(n: int, cutoff: int, seed: int, tail_seed: int) -> pd.Series:
    rng = np.random.default_rng(seed)
    base = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    tail_rng = np.random.default_rng(tail_seed)
    # A deliberately different, more volatile, independently-seeded tail.
    alt_tail = np.exp(np.cumsum(tail_rng.normal(0, 0.05, n - cutoff)))
    full = np.concatenate([base[:cutoff], base[cutoff - 1] * alt_tail])
    return pd.Series(full, index=pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC"))


@given(
    seed=st.integers(min_value=0, max_value=10_000),
    tail_seed=st.integers(min_value=0, max_value=10_000),
    n=st.integers(min_value=30, max_value=60),
    cutoff_frac=st.floats(min_value=0.3, max_value=0.8),
)
@settings(max_examples=50)
def test_trend_vol_target_unaffected_by_future_bars(
    seed: int, tail_seed: int, n: int, cutoff_frac: float
) -> None:
    cutoff = max(6, int(n * cutoff_frac))
    series_a = _diverging_series(n, cutoff, seed, tail_seed=0)  # same tail seed = identical series
    series_b = _diverging_series(n, cutoff, seed, tail_seed=max(1, tail_seed))

    pd.testing.assert_series_equal(
        series_a.iloc[:cutoff], series_b.iloc[:cutoff]
    )  # prefix sanity check

    weights_a = trend_weights(pd.DataFrame({"x": series_a}), SHORT_TREND_PARAMS)["x"]
    weights_b = trend_weights(pd.DataFrame({"x": series_b}), SHORT_TREND_PARAMS)["x"]
    pd.testing.assert_series_equal(
        weights_a.iloc[:cutoff], weights_b.iloc[:cutoff], check_exact=False, atol=1e-9
    )


@given(
    seed=st.integers(min_value=0, max_value=10_000),
    tail_seed=st.integers(min_value=1, max_value=10_000),
    n=st.integers(min_value=60, max_value=100),
    cutoff_frac=st.floats(min_value=0.4, max_value=0.8),
)
@settings(max_examples=50)
def test_xs_momentum_unaffected_by_future_bars(
    seed: int, tail_seed: int, n: int, cutoff_frac: float
) -> None:
    cutoff = max(40, int(n * cutoff_frac))
    series_a_x = _diverging_series(n, cutoff, seed, tail_seed=0)
    series_b_x = _diverging_series(n, cutoff, seed, tail_seed=tail_seed)
    # A second, independent instrument so cross-sectional ranking has
    # something to rank against.
    series_a_y = _diverging_series(n, cutoff, seed + 1, tail_seed=0)
    series_b_y = _diverging_series(n, cutoff, seed + 1, tail_seed=tail_seed)

    prices_a = pd.DataFrame({"x": series_a_x, "y": series_a_y})
    prices_b = pd.DataFrame({"x": series_b_x, "y": series_b_y})

    weights_a = xs_momentum_weights(prices_a, SHORT_XS_PARAMS)
    weights_b = xs_momentum_weights(prices_b, SHORT_XS_PARAMS)
    pd.testing.assert_frame_equal(
        weights_a.iloc[:cutoff], weights_b.iloc[:cutoff], check_exact=False, atol=1e-9
    )
