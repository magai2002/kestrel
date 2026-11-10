"""The ``Strategy`` protocol (design rule 1) and the vectorized-function
convention every strategy module in this package follows.

Every strategy module exposes two things that must agree with each
other (checked by Phase 2's parity tests):

1. A vectorized function, ``compute_weights(prices: pd.DataFrame, params)
   -> pd.DataFrame``, operating on an entire price panel at once. Used
   by ``research/vectorized.py`` and the Phase 3 walk-forward grid
   search, where speed matters more than bar-by-bar fidelity.
2. A class implementing the ``Strategy`` protocol below —
   ``on_bar(ctx) -> dict[str, float]`` — called once per closed bar by
   the event-driven engine (``backtest/``, and later ``live/``). It
   must reach the *same* decision the vectorized function would at
   that point in history, incrementally, without look-ahead.

Both read the same pydantic params schema, so a parameter grid search
(vectorized, fast) and a final event-driven validation run (slow, but
backtest-live-parity-tested) are configured identically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

import pandas as pd


@dataclass(frozen=True)
class StrategyContext:
    """What ``Strategy.on_bar`` receives.

    ``history`` holds only bars with ``close_time <= now`` (design rule
    3: a strategy sees only closed bars) — the event engine and live
    runner are responsible for never handing a strategy anything newer
    than that; this type doesn't enforce it itself since it has no
    notion of "now" beyond what's handed to it here.
    """

    history: dict[
        str, pd.DataFrame
    ]  # instrument_id -> OHLCV, ascending by open_time, up to and including "now"
    now: datetime  # close_time of the most recently closed bar


@runtime_checkable
class Strategy(Protocol):
    def on_bar(self, ctx: StrategyContext) -> dict[str, float]:
        """Target weights per instrument_id: fractions of sleeve
        equity, in [0, 1], long/flat (design rule 1). The portfolio
        layer converts these to quantities; this protocol never
        returns an order."""
        ...
