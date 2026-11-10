"""Cost model (spec Section 6.1): commission, spread, slippage, and
the Belgian stock-exchange tax (TOB).

**Verified, not trusted from the spec's own table** (rule 3 — fee
schedules move, and the spec itself flagged both of these "verify"):

- **Kraken's current entry-tier spot fees are 0.40% maker / 0.80%
  taker**, confirmed from Kraken's live fee schedule during Phase 2
  development — materially higher than the 0.25%/0.40% the spec's
  Section 6.1 table assumed. Only the taker rate is used as a default
  here: Phase 2's order manager only generates marketable orders (see
  ``execution/order_manager.py``), which take liquidity, not provide it.
- **Alpaca is commission-free**, confirmed from Alpaca's own commission
  disclosure, for "self-directed individual cash brokerage accounts
  that trade U.S.-listed securities... through an API" — this project's
  exact shape. "Relevant regulatory fees may apply" (the standard tiny
  SEC/FINRA pass-through every US broker charges, not Alpaca-specific;
  small enough to not model separately here). **Not confirmed**: FX
  fees for funding/trading from a non-USD account — Alpaca's full
  Brokerage Fee Schedule document wasn't reachable to check. Modeled as
  zero; flagged as unverified in ``docs/api-notes.md``.

Belgian TOB: per the spec, 0.35% per side on shares (capped at
EUR 1,600/transaction), 0.12% or 1.32% on ETFs depending on legal
structure — the spec's own caveat applies ("this is my tax situation
and not general advice"), so these are configured per instrument, not
asserted as fact here.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class CostModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    fee_pct: Decimal = Decimal("0.0080")  # Kraken taker, verified; equities sleeves set 0
    spread_bps: Decimal = Decimal("2")  # per-instrument config default (e.g. 2 BTC, 3 ETH)
    slippage_k: Decimal = Decimal("0.1")  # fraction of bar volatility
    tob_pct: Decimal = Decimal("0")  # 0 for crypto; equities instruments set this explicitly
    tob_cap: Decimal | None = None  # e.g. Decimal("1600") (EUR) per transaction


def half_spread_price_adjustment(price: Decimal, spread_bps: Decimal) -> Decimal:
    """Half the quoted bid-ask spread, in price units — the cost of
    crossing it, which a market/marketable order always pays."""
    return price * (spread_bps / Decimal(10_000)) / 2


def slippage_price_adjustment(
    price: Decimal, bar_volatility: Decimal, slippage_k: Decimal
) -> Decimal:
    """Volatility-scaled slippage, in price units."""
    return price * slippage_k * bar_volatility


def commission(notional: Decimal, fee_pct: Decimal) -> Decimal:
    return notional * fee_pct


def belgian_tob(notional: Decimal, tob_pct: Decimal, tob_cap: Decimal | None) -> Decimal:
    if tob_pct <= 0:
        return Decimal(0)
    tax = notional * tob_pct
    if tob_cap is not None:
        tax = min(tax, tob_cap)
    return tax
