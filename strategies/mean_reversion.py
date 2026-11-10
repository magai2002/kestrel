"""``ShortTermMeanReversion`` (spec Section 5, item 3 — a contrast case,
never deployed past ``sim``).

Buy a liquid index ETF after a sharp 2-5 day drop (RSI(2) below
``entry_rsi_threshold``, 10 by default), exit once RSI(2) recovers
above ``exit_rsi_threshold``. Its job, per the spec, is to show in the
Phase 3 report how costs and the Belgian stock-exchange tax destroy a
high-turnover edge.

RSI here uses Wilder's original smoothing (``alpha = 1/period``,
recursive, i.e. ``adjust=False``) — long-standing, stable technical
analysis math, not a vendor API that needs independent verification.

**"Exit on recovery" isn't pinned down exactly by the spec.** We define
recovery as RSI(2) crossing back above ``exit_rsi_threshold`` (50 —
back to neutral, not merely "less oversold"). A different, equally
defensible reading (price recovering to its pre-drop level, or a fixed
N-bar holding period) would change this strategy's turnover and
therefore the exact cost/TOB story it exists to tell. Flagged here as a
real interpretive choice, not silently picked.

Single-instrument by construction ("buy *a* liquid index ETF") — unlike
the other strategies, ``compute_weights`` only ever assigns a nonzero
weight to ``params.symbol``; every other column in the input panel
comes back zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from strategies.base import StrategyContext


class MeanReversionParams(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    rsi_period: int = 2
    entry_rsi_threshold: float = 10.0
    exit_rsi_threshold: float = 50.0
    position_weight: float = 1.0  # weight held while in a trade


def rsi(prices: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI. 100 when there have been no losses at all in the
    smoothing window (avg_loss == 0) rather than NaN from a division by
    zero — maximally overbought is the correct reading there, not "unknown."
    """
    delta = prices.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    value = 100.0 - 100.0 / (1.0 + rs)
    return value.where(avg_loss != 0.0, 100.0)


def _state_machine(
    rsi_values: pd.Series, entry_threshold: float, exit_threshold: float
) -> pd.Series:
    """Sequential by nature: being "in a trade" at bar t depends on the
    whole history of entries/exits before it, not just the current RSI
    value — hence a loop, not a vector op (same reasoning as
    ``research.vectorized.apply_buffer``).
    """
    position = pd.Series(0.0, index=rsi_values.index)
    in_trade = False
    for i, value in enumerate(rsi_values.to_numpy()):
        if not in_trade and not np.isnan(value) and value < entry_threshold:
            in_trade = True
        elif in_trade and not np.isnan(value) and value > exit_threshold:
            in_trade = False
        position.iloc[i] = 1.0 if in_trade else 0.0
    return position


def compute_weights(prices: pd.DataFrame, params: MeanReversionParams) -> pd.DataFrame:
    if params.symbol not in prices.columns:
        raise ValueError(f"{params.symbol!r} not found in price columns {list(prices.columns)}")
    rsi_values = rsi(prices[params.symbol], params.rsi_period)
    in_trade = _state_machine(rsi_values, params.entry_rsi_threshold, params.exit_rsi_threshold)
    weights = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    weights[params.symbol] = in_trade * params.position_weight
    return weights


@dataclass
class ShortTermMeanReversion:
    """``Strategy``-protocol wrapper with **real incremental state**
    (``_in_trade``), unlike ``TrendVolTarget``/``CrossSectionalMomentum``'s
    stateless windowed replay. Being "in a trade" can depend on
    arbitrarily old history (a trade entered months ago and never
    exited) — truncating to a recent window would silently lose track
    of an open position, which replay-from-a-window tolerates for the
    other strategies' bounded-memory signals but would be a real
    correctness bug here.
    """

    params: MeanReversionParams
    _in_trade: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        # Enough bars for Wilder's smoothing to have converged; RSI's
        # memory is short (period=2 by default), so this is a small
        # window compared to TrendVolTarget's.
        self._rsi_lookback_bars = max(20, self.params.rsi_period * 10)

    def on_bar(self, ctx: StrategyContext) -> dict[str, float]:
        bars = ctx.history.get(self.params.symbol)
        if bars is None or len(bars) < self.params.rsi_period + 1:
            return {self.params.symbol: 0.0}

        window = bars["close"].iloc[-self._rsi_lookback_bars :]
        latest_rsi = rsi(window, self.params.rsi_period).iloc[-1]
        if np.isnan(latest_rsi):
            return {self.params.symbol: 0.0}

        if not self._in_trade and latest_rsi < self.params.entry_rsi_threshold:
            self._in_trade = True
        elif self._in_trade and latest_rsi > self.params.exit_rsi_threshold:
            self._in_trade = False

        return {self.params.symbol: self.params.position_weight if self._in_trade else 0.0}
