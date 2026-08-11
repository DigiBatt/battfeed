# Import logged files

Some sources ingest **complete files** rather than polling a live device — a folder of exported flight logs, say. `battfeed import` drains such a source through the same `DataSource` seam, writing through a `RoutingSink` so the result is one `.bdf.csv` (plus sidecar) per `(series_id, run_id)` — one file per pack and flight, for the DJI source:

```bash
battfeed import --source dji --opt path=C:/logs/dji --out-dir imported
```

One-shot by default — it stops when the source reports drained and prints exactly what it wrote (or "nothing to import"); pass `--watch` to keep polling the folder for new files until Ctrl-C.

## The delivery guarantee

Import is **at-least-once**: a batch source records a file as ingested only *after* its rows are safely written, so a crash mid-import re-imports the file into new segment files rather than losing it. Dedupe and quarantine are backed by an `ImportLedger` (sha256 content hashes):

- a file already imported (by content, not name) is skipped;
- a permanently-unsupported file is **quarantined** with a recorded reason and never retried;
- deleting output files does not reset the ledger — `battfeed import --reset-ledger` does.

Why it works this way, and why "never lose data quietly" is a design rule rather than a feature, is covered in [reliability](../explanation/reliability.md).

## The DJI source specifically

`dji` wraps the external [`dji-log`](https://github.com/lvauvillier/dji-log-parser) binary (on your PATH or via `DJI_LOG_BIN`); no Python extra is needed. Record files come from untrusted media, so battfeed passes paths to the binary safely and quarantines anything malformed. Decrypting v13+ records calls DJI's keychain API, which needs an API key — put it in a [config file with `${ENV:...}` expansion](config-files.md), not on the command line. GPS columns are off by default (`--opt include_gps=true` opts in), since location data deserves a deliberate decision.

## Writing an import source

An importer is just a `DataSource` whose `poll()` yields the rows of the next pending file, stamping `series_id`/`run_id` on each sample and its own per-run `test_time_second`. If you can parse it, battfeed can import it; the [custom-source tutorial](../tutorials/custom-source.md) covers the seam, and `ImportLedger` is public API for your own dedupe.
