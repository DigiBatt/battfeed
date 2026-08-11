# Choose a sink

A **sink** is where collected samples land. `BdfCsvSink` (one BDF file for one test object) is the default and right for most bench work; battfeed ships three more for the cases it is not.

## One stream, many objects: `RoutingSink`

When one connection yields samples from several physical objects or test runs — a fleet gateway, per-flight logs — sources stamp two reserved keys that say *where a sample belongs* rather than *what was measured*: `series_id` (which object) and `run_id` (which test-run segment). `RoutingSink` demultiplexes on them: it sanitises raw device serials into safe BDF cell names, opens each object's file lazily on its first sample, optionally rotates by time or row count, and reports what it wrote via `files_by_series`.

```python
from battfeed import Harvester, RoutingSink

sink = RoutingSink(out_dir="fleet", institution="SINTEF")
Harvester().collect("my-gateway", duration_s=3600, interval_s=5, sink=sink)
print(sink.files_by_series)
```

A source that emits routing keys must supply its own `test_time_second`, zero-based per `(series_id, run_id)` — the harvester's shared elapsed-collection clock is wrong for an object that appears mid-run. Note the split in key handling: `BdfCsvSink` **strips** routing keys (they are never BDF columns), while the Parquet and HTTP sinks **keep** them so downstream code can group by object and run.

## Feed a server: `HttpPushSink`

POSTs samples as gzipped newline-delimited JSON to any HTTP endpoint on a time cadence, stdlib-only. Delivery is at-least-once: rows are kept on a failed POST and spooled to a loadable `.spool.ndjson` if the endpoint stays down, so a dead server costs you latency, never data. Use it to feed a registry or lab server; it is a *sink*, not a hosted service — battfeed runs no server ([why](../explanation/design.md)). Put the endpoint token in a [config file](config-files.md).

## Analyze in pandas/Polars/DuckDB: `ParquetSink`

```bash
pip install "battfeed[parquet]"
```

One Parquet file (plus the same `.meta.json` sidecar) per bounded capture. Use it for finite analytical captures, not unbounded telemetry — that is `RoutingSink` + BDF.

## Rules that hold for every sink

Whatever the sink, the sidecar contract is the same (`finalized` flips true only on clean close), the sign convention is BDF's (positive current charges), and the harvester's error policy stands between the source and the sink. The full output contract is in the [reference](../reference/output.md).
