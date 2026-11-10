"""Portfolio layer: weights -> target quantities (design rule 1).

``sizing.py`` holds the volatility-targeting primitive shared with
``strategies/trend_vol_target.py`` (one formula, not duplicated across
a strategy and a portfolio module) and position buffering applied to
the *combined* per-sleeve target before it becomes an order — the same
buffering concept as ``research/vectorized.py::apply_buffer``, but
comparing against the weight actually implied by current holdings
rather than a remembered previous target, since the event-driven engine
has real positions to ask.
"""
