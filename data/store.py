"""Parquet writer + DuckDB reader for market data (spec Section 4.4).

Partition layout on disk is Hive-style::

    {data_dir}/venue=<venue>/symbol=<symbol>/timeframe=<tf>/year=<year>/bars.parquet

``venue``, ``symbol``, ``timeframe`` and ``year`` are deliberately *not*
duplicated inside the Parquet file's own columns — DuckDB's
``hive_partitioning`` reconstructs them from the directory path on read.
Storing them twice would invite the two copies to disagree.

Symbols must not contain ``/`` (a directory separator would turn one
partition into a nested tree). Kraken's ccxt-unified symbols like
``BTC/USD`` are spelled ``BTC-USD`` everywhere in this project outside
the ccxt API call itself; see ``data/sources/ccxt_ohlcv.py``.

Writes are idempotent per partition: re-fetching a year overwrites that
one file rather than appending, so a corrected month replaces stale
data instead of accumulating duplicates.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from data import BAR_COLUMNS

_PARTITION_KEYS = ("venue", "symbol", "timeframe")
_FILE_COLUMNS = tuple(c for c in BAR_COLUMNS if c not in _PARTITION_KEYS)


def _partition_dir(data_dir: Path, venue: str, symbol: str, timeframe: str, year: int) -> Path:
    if "/" in symbol:
        raise ValueError(f"symbol {symbol!r} contains '/'; use '-' instead (see module docstring)")
    return (
        data_dir / f"venue={venue}" / f"symbol={symbol}" / f"timeframe={timeframe}" / f"year={year}"
    )


def write_bars(df: pd.DataFrame, data_dir: Path) -> list[Path]:
    """Write bars to the partitioned Parquet store.

    One file per (venue, symbol, timeframe, year) group found in ``df``;
    a single call may cover several groups (e.g. a multi-year backfill).
    Returns the list of files written.
    """
    missing = set(BAR_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"missing columns: {sorted(missing)}")
    if df.empty:
        return []

    written: list[Path] = []
    sorted_df = df.sort_values("open_time")
    for (venue, symbol, timeframe), group in sorted_df.groupby(list(_PARTITION_KEYS), sort=False):
        for year, year_group in group.groupby(group["open_time"].dt.year):
            out_dir = _partition_dir(data_dir, str(venue), str(symbol), str(timeframe), int(year))
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / "bars.parquet"
            year_group.loc[:, list(_FILE_COLUMNS)].to_parquet(out_path, index=False)
            written.append(out_path)
    return written


def upsert_bars(df: pd.DataFrame, data_dir: Path) -> list[Path]:
    """Merge ``df`` into the existing partitions instead of replacing
    them: the live feeds persist a handful of new bars per cycle, and
    ``write_bars`` would overwrite a whole year's file with just those.
    Where both have a bar for the same ``open_time``, the new one wins
    (a venue's later, corrected print beats an earlier one).
    """
    missing = set(BAR_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"missing columns: {sorted(missing)}")
    if df.empty:
        return []
    written: list[Path] = []
    for (venue, symbol, timeframe), group in df.groupby(list(_PARTITION_KEYS), sort=False):
        for year, year_group in group.groupby(group["open_time"].dt.year):
            out_dir = _partition_dir(data_dir, str(venue), str(symbol), str(timeframe), int(year))
            out_path = out_dir / "bars.parquet"
            new = year_group.loc[:, list(_FILE_COLUMNS)]
            if out_path.exists():
                old = pd.read_parquet(out_path)
                old["open_time"] = pd.to_datetime(old["open_time"], utc=True)
                old["close_time"] = pd.to_datetime(old["close_time"], utc=True)
                old = old[~old["open_time"].isin(new["open_time"])]
                new = pd.concat([old, new], ignore_index=True)
            new = new.sort_values("open_time").reset_index(drop=True)
            out_dir.mkdir(parents=True, exist_ok=True)
            tmp = out_path.with_suffix(".parquet.tmp")
            new.to_parquet(tmp, index=False)
            tmp.replace(out_path)  # atomic: a crash mid-write never leaves a torn file
            written.append(out_path)
    return written


def read_bars(
    data_dir: Path,
    venue: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> pd.DataFrame:
    """Read bars back via DuckDB, optionally filtered by venue/symbol/timeframe.

    Returns an empty, correctly-columned frame (not an error) if the
    store is empty or the filter matches nothing.
    """
    empty = pd.DataFrame(columns=list(BAR_COLUMNS))
    # With the full partition key given, glob only that partition's files
    # instead of every file in the store (the live runner reads one
    # listing at a time, every cycle). DuckDB still parses key=value
    # segments from the full path, so the hive columns are unchanged.
    root = data_dir
    if venue is not None and symbol is not None and timeframe is not None:
        root = data_dir / f"venue={venue}" / f"symbol={symbol}" / f"timeframe={timeframe}"
    if not root.exists() or next(root.rglob("*.parquet"), None) is None:
        return empty

    pattern = str(root / "**" / "*.parquet")
    conditions = []
    params: list[str] = []
    if venue is not None:
        conditions.append("venue = ?")
        params.append(venue)
    if symbol is not None:
        conditions.append("symbol = ?")
        params.append(symbol)
    if timeframe is not None:
        conditions.append("timeframe = ?")
        params.append(timeframe)

    query = f"SELECT * FROM read_parquet('{pattern}', hive_partitioning=true)"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY open_time"

    con = duckdb.connect()
    try:
        result = con.execute(query, params).fetchdf()
    finally:
        con.close()

    if result.empty:
        return empty
    result["open_time"] = pd.to_datetime(result["open_time"], utc=True)
    result["close_time"] = pd.to_datetime(result["close_time"], utc=True)
    result = result.drop(columns=["year"], errors="ignore")
    return result[list(BAR_COLUMNS)]
