"""The ``Broker`` protocol (spec Section 3.2 / design rule 1): strategy,
portfolio, risk and order-manager code is identical across all four run
modes — only the clock, data feed and broker adapter change. Every
broker, sim or real, is approached through this one interface.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from core.bar import Bar
from core.fill import Fill
from core.order import Order


class Broker(Protocol):
    def submit_order(self, order: Order, bar: Bar, trailing_volatility: Decimal) -> Fill | None:
        """Attempt to fill ``order`` against ``bar``.

        ``trailing_volatility`` is a volatility estimate computed from
        bars *strictly before* ``bar`` — deliberately not derived from
        ``bar`` itself (e.g. its own high-low range), which would let a
        fill priced at this bar's open use information from later in
        the same bar that hadn't happened yet at that moment. The
        engine computes it from history, same as a strategy would.

        Returns ``None`` if nothing filled this bar (e.g. zero volume).
        """
        ...
