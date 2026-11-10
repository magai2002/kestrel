"""Risk: pre-trade checks (``pre_trade.py``) and kill switches
(``kill_switch.py``), each configured by ``limits.py`` (spec Section
6.3). Pre-trade checks reject one order at a time with a reason;
kill switches persist a tripped state until reset by hand (Phase 4+'s
admin CLI — in the backtest path there's no hand to reset it, so a
tripped kill switch simply halts new positions for the rest of the run).
"""
