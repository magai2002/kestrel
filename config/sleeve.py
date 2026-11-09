"""Pydantic schema and loader for per-sleeve YAML configs (Section 3.1).

Strategy parameter schemas are not here yet: there are no strategies
until Phase 1, and a schema with nothing to validate is just noise.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, PositiveFloat

from core.enums import AssetClass, Timeframe, Venue


class UniverseEntry(BaseModel):
    """One instrument in a sleeve's universe."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    venue: Venue
    base_currency: str
    quote_currency: str


class SleeveConfig(BaseModel):
    """One sleeve: its own account, capital, universe and decision bar."""

    model_config = ConfigDict(frozen=True)

    name: str
    asset_class: AssetClass
    decision_bar: Timeframe
    base_currency: str
    paper_capital: PositiveFloat
    universe: list[UniverseEntry]


def load_sleeve_config(path: Path) -> SleeveConfig:
    """Load and validate a sleeve YAML file.

    Raises ``pydantic.ValidationError`` with a precise field-level
    message if the file is malformed — fail loudly rather than silently
    coercing, per the spec's lesson about free APIs/config changing
    shape underneath you.
    """
    raw = yaml.safe_load(path.read_text())
    return SleeveConfig.model_validate(raw)
