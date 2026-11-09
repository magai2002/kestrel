# 0002: Parquet + DuckDB for market data, not the existing InfluxDB

## Context

The VPS this project deploys to already runs an InfluxDB instance (org
`quantfinance`, bucket `market_data`), written to by an existing,
unrelated market-data collector service. `kestrel` needs its own
historical OHLCV store for backtesting, walk-forward validation and
reproducible research — reusing the existing InfluxDB would mean one
less piece of infrastructure to run.

## Options considered

1. **Reuse the existing InfluxDB** instance and bucket.
2. **A separate Parquet file store, partitioned by
   `venue/symbol/timeframe/year`, queried via DuckDB.**
3. A separate Postgres (or TimescaleDB) time-series table.

## Decision

Option 2: Parquet partitioned by `venue/symbol/timeframe/year`, read
with DuckDB.

## Rationale

- **It's shared with another project.** Section 2 of the spec is
  explicit: leave the existing InfluxDB alone. Writing `kestrel`'s own
  OHLCV history into it would couple two unrelated projects' schemas,
  retention policies and failure modes. A bug in one collector's write
  path shouldn't be able to corrupt the other's backtest inputs.
- **Reproducibility is a hard requirement here that it isn't for a
  live dashboard time series.** Design rule 8 (spec Section 4.3) says
  every backtest run records a data hash alongside the git SHA and
  config hash. That only means something if the underlying data is a
  fixed, content-addressable artifact — a running database you can
  keep writing to and backfilling into is a much worse fit for "hash
  this and prove nothing changed" than a set of immutable files.
- **DuckDB reads Parquet directly and fast**, with zero running
  server process, which matches research/backtest access patterns
  (large scans, columnar aggregation) far better than a time-series
  database tuned for high-frequency point writes and recent-data
  queries.
- Parquet files are trivially versionable, diffable by partition, and
  cheap to ship a small slice of for the Phase 6 `make demo` committed
  sample — an InfluxDB export is not.

## Consequences

- Two storage systems now exist on the same box for "market data":
  the existing InfluxDB (a different project's concern, untouched) and
  `kestrel`'s own Parquet store. This is intentional, not an oversight.
- `kestrel` does not get InfluxDB's continuous-query/downsampling
  tooling or Grafana's native Influx data source. Grafana dashboards
  for `kestrel` (Phase 5) read Prometheus metrics instead (Section 9),
  not the Parquet store directly — Parquet/DuckDB is a research-time
  store, not a live-dashboard time series.
- Partition layout (`venue=.../symbol=.../timeframe=.../year=...`)
  must stay Hive-style for DuckDB's `hive_partitioning` to reconstruct
  those columns on read; `data/store.py` enforces symbols can't contain
  `/`, since that would silently create a nested, wrong partition
  instead of erroring.
