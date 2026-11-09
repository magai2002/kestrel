"""Domain types shared across every mode (backtest, sim, sandbox, live).

Phase 0 only needs the types the data layer touches: instrument
identity and OHLCV bars. Order, Fill, Position, the event types and the
Clock protocol belong to execution and arrive in Phase 2, when there is
an order manager to exercise them.
"""
