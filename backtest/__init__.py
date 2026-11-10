"""The event-driven backtest engine (spec Section 4.1's full event
flow) and its results object. Strategy, portfolio, risk and
order-manager code here is the *exact same code* ``live/`` will use in
Phase 4 (design rule 1) — only the clock, data feed and broker adapter
differ, and this phase's clock/feed/broker are ``SimClock``, stored
Parquet history, and ``SimBroker``.
"""
