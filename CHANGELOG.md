# Changelog

All notable changes to this project are documented in this file.

The format is based on Keep a Changelog and this project follows Semantic Versioning.

## [Unreleased]

### Added

- Reserved routing keys in the sample contract: `protocols.RESERVED_KEYS`
  (`series_id`, `run_id`) document *which physical object* and *which run
  segment* a sample belongs to. They are routing metadata, never BDF columns,
  and a source that emits them must supply its own zero-based-per-(series, run)
  `test_time_second` (invariant I5). Groundwork for the forthcoming
  `RoutingSink`.
- `RoutingSink` (`battfeed.RoutingSink`): demultiplexes one sample stream into
  one BDF file per (`series_id`, `run_id`), with time- and/or row-based
  rotation for unbounded streams (segments are runs; each closed segment is
  finalized with its sidecar immediately). Includes a `series_info` hook for
  per-object filenames/metadata, deterministic case-insensitive sanitization
  of raw series ids into BDF cell names (`battfeed.sinks.sanitize_cell_name`;
  colliding ids get a stable hash suffix -- the sidecar's raw `series_id`,
  not the filename, is the stable join key), a single default stream for
  routing-free samples, per-series file reporting via `files_by_series`, and
  injectable `sink_factory`/`clock`/`today` for tests. Dataset paths are
  reserved atomically (`O_CREAT|O_EXCL`) at allocation, so concurrent
  collections into one directory can never claim the same file.
  Source-supplied `test_time_second` passes through untouched (invariant I5).
- `Harvester.collect` now warns (once per source per run) when it has to
  stamp its shared elapsed-collection `test_time_second` onto a sample that
  carries routing keys -- such a source violates invariant I5 and gets a
  wrong timebase for objects appearing mid-run.
- The `battfeed import` verb (`battfeed.run_import`): a batch-import driver
  that drains file-ingesting sources through the same `DataSource` seam --
  one-shot until the source reports drained (optional `drained()` hook,
  which requires a stop event) or `--watch` until Ctrl-C -- writing through
  a `RoutingSink` (one `.bdf.csv` + sidecar per (series, run)). Import
  delivery is **at-least-once**: the driver calls the source's optional
  `commit_batch()` hook only after a batch is safely written, so a crash
  mid-import re-imports the file into new segment files rather than
  silently losing it. The driver warns when imported rows lack
  `test_time_second` (invariant I5), and all waits are stop-responsive, so
  Ctrl-C interrupts even a long backoff immediately.
- `battfeed.ImportLedger`: a JSON-backed, sha256-content-keyed dedupe
  ledger and quarantine for import sources (single-writer; zero-byte files
  are never content-keyed; skips logged; `hash_of()`/`content_hash=` to
  hash each file once), with atomic unique-temp-file writes.
  `--reset-ledger` clears it via a source's `reset_ledger()` hook --
  deleting output files never does, and a corrupt ledger file must be
  deleted manually.
- `StreamingSource`, a base for push-style sources (BLE/CAN/MQTT):
  background reader sessions with a bounded, counted-overflow buffer (I2);
  reader errors re-raise at `poll()`, and sample-less sessions raise the new
  `DeadReaderError` on restart so a dead device escalates through the
  harvester's `ErrorPolicy` to `SourceFailure` (I4) instead of resetting it;
  generation-guarded sessions discard late contributions from abandoned
  zombie readers.
- The shipped `battfeed.testing` kit: JSONL record/replay tapes
  (`ReplayTape` with offset validation and torn-tail tolerance on load,
  `ReplayReader` with time compression, `TapeRecorder` tee helper) and
  `check_source` -- strict-JSON, bool-, aliasing- and routing-discipline
  contract checks for third-party source test suites, with a documented
  list of deliberate blind spots.
- `HttpPushSink`: generic, stdlib-only HTTP ingest sink -- gzipped
  newline-delimited JSON batches on a time cadence to any endpoint, with
  bearer/custom headers (CRLF-validated at construction), at-least-once
  delivery (buffer kept on failure; `close()` retries then spools to a
  loadable `.spool.ndjson` with cwd fallback and never raises), strict
  RFC 8259 payloads (non-finite floats become null, bytes base64,
  datetimes ISO 8601), bounded memory (oldest rows spill to spool), and
  URL redaction in logs. Routing keys are deliberately included for
  server-side demultiplexing.
- `ParquetSink` (new optional extra `battfeed[parquet]`): one atomically
  written Parquet file plus the standard `.meta.json` sidecar per bounded
  capture; on any write failure every buffered row lands in a rescue
  `.ndjson` and the sidecar records the error -- a capture is never lost.
- CI: a `quality-extras` job installs all installable source extras per-OS
  (the extras-free matrix remains the hard-import guard, invariant I6) and
  smoke-tests the CLI end to end. New `docs/running-unattended.md` with
  systemd, Windows Task Scheduler, and NSSM recipes, sidecar
  `finalized`-flag semantics, and log-rotation guidance.
- `battfeed collect`/`import` gain `--config FILE`: a TOML config supplying
  source options (`[source.<name>]`, optional `type` to alias a source) and
  run parameters (`[collect]`/`[import]`), so a long invocation becomes a
  file. Precedence: `--opt` > `[source.<name>]` > source default (kwargs);
  CLI flag > `[collect]`/`[import]` > default (run params). String values may
  embed `${ENV:VAR}`, expanded from the environment when the section is used
  -- keeping secrets out of the file. `tomllib` is stdlib on 3.11+; on 3.10
  install `tomli` (no hard dependency added). New public API:
  `battfeed.load_config`, `Config`, `ConfigError`. `--no-watch` overrides a
  config `watch = true`.
- Credential hygiene, two layers: options named like a credential (by
  segment-anchored match on `key`/`token`/`secret`/`password`/`passphrase`/
  `auth`/`authorization`/`bearer`/`credential`/`session`/`cookie`/
  `signature`/`private`/`pat`/`pin`/`otp`/`salt` -- so `path`/`compatibility`
  stay clear) are masked (`***`), AND resolved secret *values* plus
  `user:pass@` URL userinfo are scrubbed by value even under an innocuous
  key. Coverage spans the `.meta.json` sidecar (at the sink boundary, so
  `RoutingSink` inherits it), captured logs at any level including exception
  tracebacks and every logger's handlers, CLI error messages, and the
  `battfeed sources` listing; a source's env-var secret fallback (e.g.
  `DJI_API_KEY`) is folded into the scrubber. Reusable
  `battfeed.config.redact_mapping`/`redact_text`/`is_secret_key`.
- `dji` source: import DJI Fly app flight records (`*.txt`/`*.dat`, suffix
  case-insensitive) as per-(pack, flight) BDF feeds via `battfeed import`.
  Wraps the external `dji-log` CLI; emits voltage/current (negated to BDF
  sign)/power/temperature plus per-cell `cell_N_voltage_volt` extension
  columns, routed by series_id=`<aircraft>:<battery>` (content-hash
  fallback for missing serials) and run_id=`flight-<hash>`.
  `test_time_second` is zero-based per flight and monotonic (rows sorted by
  timestamp); non-finite values are dropped. Untrusted-input hardening: the
  record path is passed absolutized after a `--` end-of-options token; the
  API key is redacted from every log and error; `.DAT`, unparseable, and
  malformed-output files are quarantined (never retried to failure), so one
  bad file cannot starve the folder. Requires the `dji-log` binary
  (`DJI_LOG_BIN`/PATH); decrypting v13+ records makes a network call to
  DJI's keychain API.

### Changed

- `BdfCsvSink` strips the reserved routing keys from every row (and from any
  explicit `columns` set) before header inference and writing, so a
  routing-aware source wired to the plain sink cannot leak them into CSV
  columns.
- `BdfCsvSink` writes its `.meta.json` sidecar **early** — as soon as the data
  file is first opened, marked `"finalized": false` — rewrites it periodically
  as rows accumulate, and finalises it (`"finalized": true`, final row count)
  on `close()`. A crash mid-collection now leaves a valid data file *with*
  metadata on disk; the new `finalized` flag distinguishes an interrupted run
  from a clean one. The sidecar is written via a temp-file-and-atomic-replace
  so readers never see a half-written file.

## [0.4.0] - 2026-07-08

Renamed from `gleaned` to **battfeed** (the package is domain-anchored; a
package name is not a metaphor — cf. `batterydf` ≠ BDF). No behaviour change;
the two renames below are breaking for plugin authors and sidecar readers, but
there are no known consumers (the package was never published to PyPI).

### Changed (breaking)

- Entry-point group `gleaned.sources` → `battfeed.sources`: third-party source
  plugins must update their `[project.entry-points]` group name.
- Sidecar key `gleaned_version` → `battfeed_version`: `.meta.json` files written
  from 0.4.0 on carry the new key. Sidecars already written by ≤0.3.0 field
  runs keep the old `gleaned_version` key.
- Console script, import package, and PyPI distribution are now `battfeed`
  (`import gleaned` no longer exists).

## [0.3.0] - 2026-07-07 (as gleaned)

Field-duty release: resilience, open-ended collection, configurable sources,
and two real device collectors ported (with the owner's authorization) from
a proprietary platform, which retires its own copies in favour of gleaned.

### Added

- `ErrorPolicy` and `SourceFailure`: transient `poll()` failures are retried
  with exponential backoff; runs are only abandoned after too many
  consecutive failures. `CollectStats.errors` counts tolerated failures.
- Open-ended collection: `Harvester.collect(duration_s=None, stop=...)` runs
  until stopped; the CLI collects until Ctrl-C when `--duration` is omitted.
- `gleaned collect --opt KEY=VALUE` (repeatable, JSON-coerced values) passes
  constructor options to sources; `gleaned sources` now lists each source's
  options.
- `Sample`/`SampleValue` type aliases; string values permitted for auxiliary
  columns (e.g. charge status flags).
- New source: `mc3000` — SkyRC MC3000 charger/analyzer (frame protocol,
  BLE/USB/mock transports; one slot per instance, matching BDF's
  one-cell-per-file model). Extras: `gleaned[mc3000-ble]`, `gleaned[mc3000-usb]`.
- New source: `android` — Android device battery via `adb` (dumpsys + sysfs),
  pure stdlib; the BDF sign convention is enforced from the charge status.
- CI workflow (ruff, ruff format, mypy, pytest on Linux + Windows,
  Python 3.10-3.12) and this changelog.

### Changed

- `BdfCsvSink` flushes after every batch so long field runs survive crashes.
- Built-in source discovery is resilient: one broken source no longer breaks
  `gleaned sources` for the rest.

### Hardware shakedown (verified against a physical MC3000 over BLE)

- `Mc3000Source` gained `time_base` (`"collection"` default | `"device"`):
  an idle bay reports a constant-zero program timer, so the harvester's
  collection clock is now the default timebase and the device run timer is
  opt-in for program-aligned captures.
- BLE transport settles 0.35 s after subscribing before the first write —
  the HM-10 bridge drops a write sent immediately after `start_notify`.
- Intentional `close()` no longer logs a spurious "will reconnect" warning.
- Machine-info is attempted once per source: the tested firmware never
  answers the opcode (zero reply bytes), so retries only delayed run start.
- Field note: the unit advertises as **"Charger"** (no "MC3000" in the BLE
  name); discover it by the FFE0 service UUID, not by name.

## [0.2.0] - 2026-07-07 (as gleaned)

- Complete rebuild as the source→BDF collector toolkit: `DataSource`/`Sink`
  protocols, `Harvester`, simulator/csvtail/WMI sources, `BdfCsvSink` with
  metadata sidecar, entry-point plugin discovery, `gleaned` CLI, 23 tests.
- License: GPL-3.0 → Apache-2.0 (with NOTICE).

## [0.1.0] - 2024-12-16 (as gleaned)

- Initial prototype (harvester skeleton and Windows WMI telemetry).
