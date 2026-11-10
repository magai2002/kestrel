"""``SimBroker``: fills orders against historical bars using the cost
and fill models in ``execution/costs.py`` / ``execution/fills.py``.

Market orders only, for now — ``execution/fills.py``'s docstring
explains why passive limits aren't wired into the order manager yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from core.bar import Bar
from core.fill import Fill
from core.order import Order
from execution.costs import CostModel
from execution.fills import build_market_fill


@dataclass
class SimBroker:
    cost_model: CostModel = field(default_factory=CostModel)
    participation_cap: Decimal = Decimal("0.05")

    def submit_order(self, order: Order, bar: Bar, trailing_volatility: Decimal) -> Fill | None:
        return build_market_fill(
            order=order,
            open_price=Decimal(str(bar.open)),
            bar_volume=Decimal(str(bar.volume)),
            bar_volatility=trailing_volatility,
            cost_model=self.cost_model,
            participation_cap=self.participation_cap,
            timestamp=bar.close_time,
        )
