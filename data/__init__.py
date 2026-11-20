"""Market data: fetch, store, validate.

Every function in ``data/sources/`` returns the same shape regardless of
venue — a :class:`pandas.DataFrame` with columns ``venue``, ``symbol``,
``timeframe``, ``open_time``, ``close_time``, ``open``, ``high``,
``low``, ``close``, ``volume`` (UTC tz-aware timestamps, float prices;
see ``core.bar.Bar`` for why floats here). ``store.py`` and
``validate.py`` only ever see that shape, never a venue-specific one.
"""

BAR_COLUMNS: tuple[str, ...] = (
    "venue",
    "symbol",
    "timeframe",
    "open_time",
    "close_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
)
