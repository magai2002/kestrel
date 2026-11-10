"""Research tools: performance statistics and a vectorized quick
backtester, used for first-look analysis and (from Phase 3 on) the
walk-forward validation pipeline's inner loop, where speed matters more
than the event-driven engine's bar-by-bar fidelity.

Everything here operates on plain pandas Series/DataFrames of floats —
design rule 4 reserves ``Decimal`` for money that actually moves
(orders, fills, the ledger); signal maths and research statistics are
explicitly float-friendly.
"""
