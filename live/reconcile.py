"""Reconciler (spec Section 4.1's REC box): our ledger vs the venue.

Every cycle, for every asset: does the venue hold ``baseline + ledger``
units (within one step size, or ``tolerance_pct``)? And does the venue
have at least the cash the ledger thinks the sleeve has? Any "no" trips
the reconciliation kill switch. A mismatch means the bot's picture of
the world is wrong — a fill we missed, a manual trade in the account, a
testnet wipe — and trading on a wrong picture is how small bugs become
large losses. Stop, alert, let a human look.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class ReconcileResult:
    mismatches: dict[str, str] = field(default_factory=dict)
    venue_positions: dict[str, Decimal] = field(default_factory=dict)
    expected_positions: dict[str, Decimal] = field(default_factory=dict)
    venue_cash: Decimal | None = None
    ledger_cash: Decimal | None = None

    @property
    def ok(self) -> bool:
        return not self.mismatches


def reconcile(
    ledger_positions: dict[str, Decimal],  # asset id -> qty
    symbols: dict[str, str],  # asset id -> venue symbol
    baseline: dict[str, Decimal],  # venue symbol -> qty held before the sleeve started
    venue_positions: dict[str, Decimal],  # venue symbol -> qty
    step_sizes: dict[str, Decimal],  # asset id -> step
    tolerance_pct: float,
    ledger_cash: Decimal | None = None,
    venue_cash: Decimal | None = None,
) -> ReconcileResult:
    mismatches: dict[str, str] = {}
    expected: dict[str, Decimal] = {}
    tol = Decimal(str(tolerance_pct))
    for aid, qty in ledger_positions.items():
        sym = symbols[aid]
        exp = baseline.get(sym, Decimal(0)) + qty
        expected[sym] = exp
        got = venue_positions.get(sym, Decimal(0))
        allowed = max(step_sizes.get(aid, Decimal(0)), abs(exp) * tol)
        if abs(got - exp) > allowed:
            mismatches[sym] = (
                f"venue holds {got}, ledger expects {exp} (baseline {baseline.get(sym, 0)} + {qty})"
            )
    if ledger_cash is not None and venue_cash is not None:
        slack = abs(ledger_cash) * tol + Decimal("0.01")
        if venue_cash + slack < ledger_cash:
            mismatches["cash"] = f"venue free cash {venue_cash} < ledger cash {ledger_cash}"
    return ReconcileResult(mismatches, dict(venue_positions), expected, venue_cash, ledger_cash)
