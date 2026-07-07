# Changelog

All notable changes to this project are documented in this file.

The format is based on Keep a Changelog and this project follows Semantic Versioning.

## [0.3.0] - 2026-07-07

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

## [0.2.0] - 2026-07-07

- Complete rebuild as the source→BDF collector toolkit: `DataSource`/`Sink`
  protocols, `Harvester`, simulator/csvtail/WMI sources, `BdfCsvSink` with
  metadata sidecar, entry-point plugin discovery, `gleaned` CLI, 23 tests.
- License: GPL-3.0 → Apache-2.0 (with NOTICE).

## [0.1.0] - 2024-12-16

- Initial prototype (harvester skeleton and Windows WMI telemetry).
