"""Broker adapters, all behind the same ``Broker`` protocol (spec
Section 3.2): only the broker (and clock, and data feed) changes
between run modes. ``sim.py`` is the only implementation that exists
yet; ``binance_testnet.py``, ``kraken.py`` and ``alpaca.py`` arrive in
Phase 4.
"""
