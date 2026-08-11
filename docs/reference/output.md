# Output contract

What battfeed writes is the whole point of battfeed, so the file contract is precise.

## File naming

```
InstitutionCode__CellName__YYYYMMDD_NNN.bdf.csv
InstitutionCode__CellName__YYYYMMDD_NNN.meta.json
```

The sequence number `NNN` is the next free number for that institution/cell/day (capped at `999`, which bounds `--duration` for 24/7 services — see [unattended operation](../howto/run-unattended.md)). The generator is public: `battfeed.sinks.bdf_csv.dataset_filename`. `RoutingSink` derives the cell-name part from a sanitised `series_id`, and appends run segmentation.

## Columns

A conforming BDF CSV with snake_case `{quantity}_{unit}` headers. The required trio always leads the header, in this order:

```
test_time_second, voltage_volt, current_ampere
```

followed by any extra columns in alphabetical order. If a source does not stamp its own `test_time_second`, the harvester stamps each sample with elapsed collection time.

## Sign convention

Per the BDF specification: **positive current charges the test object; negative current discharges it.** Power follows the same sign. Every built-in source converts its device's native convention to this one before emitting; sources you write must do the same.

## The sidecar

Every data file gets a `<name>.meta.json` sidecar recording how the data was collected: the run's institution/cell, the source's `metadata()` self-description (secrets redacted), requested duration and interval, start/finish timestamps, the battfeed version, the column list, the row count, and the `finalized` flag.

The sidecar is written **early** (as soon as the data file is opened, with `"finalized": false`), refreshed during collection, and rewritten with `"finalized": true` plus final counts on every clean exit, including Ctrl-C and source-failure exits. On disk that means:

- `"finalized": true` — the run ended under program control; the pair is complete and safe to ship.
- `"finalized": false` — the process is still running, or was killed hard. Rows are flushed after every poll, so the data itself is valid up to roughly the last sample; only the "this file is done" marker is missing.

Downstream movers should select pairs by sidecar, picking up only `"finalized": true`.

## Routing keys

Two optional reserved keys on a sample (`battfeed.RESERVED_KEYS`) say *where it belongs* rather than *what was measured*: `series_id` (which physical object) and `run_id` (which test-run segment). They are never BDF columns — `BdfCsvSink` strips them; `ParquetSink` and `HttpPushSink` keep them (as a column and a payload field respectively) so downstream code can group by object and run. A source emitting them must supply its own per-`(series_id, run_id)` zero-based `test_time_second`.

## Other sinks' outputs

`ParquetSink` writes one Parquet file per bounded capture with the same sidecar contract. `HttpPushSink` POSTs gzipped newline-delimited JSON batches, spooling to `.spool.ndjson` when the endpoint is down. Choosing between them: [Choose a sink](../howto/choose-a-sink.md).
