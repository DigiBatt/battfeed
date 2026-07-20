# battfeed

**battfeed turns live battery data sources into BDF (Battery Data Format) feeds.**

It is the acquisition layer of an open battery-data stack: point it at something that
produces battery readings — a simulator, a growing instrument log, the battery in your
Windows laptop, or your own cycler driver — and it polls that source on a schedule and
writes conforming `.bdf.csv` files (plus a `.meta.json` sidecar) that the rest of the
stack understands.

```
  cyclers · instruments · OS batteries · live logs · simulators
                        │
                        │  poll()                (you implement DataSource)
                        ▼
                     battfeed                     (this package: Harvester + sinks)
                        │
                        ▼
        *.bdf.csv  +  *.meta.json                (Battery Data Format files)
                        │
        ┌───────────────┼────────────────────┐
        ▼               ▼                    ▼
    batterydf        BattINFO           BDA registry
  (normalize,      (semantics,           (publish,
   validate,        ontology)             share)
   analyze)
```

The core has **zero runtime dependencies** — everything is Python standard library.
Requires Python >= 3.10.

## Install

```
pip install battfeed
```

| Extra                    | Installs    | Enables                                            |
| ------------------------ | ----------- | -------------------------------------------------- |
| `pip install battfeed[wmi]` | `wmi`       | `WmiBatterySource` (Windows laptop/tablet battery) |
| `pip install battfeed[mc3000-ble]` | `bleak` | `Mc3000Source` over Bluetooth LE (the `mock` transport needs no extra) |
| `pip install battfeed[mc3000-usb]` | `pyusb` | `Mc3000Source` over USB |
| `pip install battfeed[parquet]` | `pyarrow` | `ParquetSink` — write captures as Parquet for analysis |
| `pip install battfeed[bdf]` | `batterydf` | `validate_file()` — check emitted files against the BDF reference implementation |
| `pip install battfeed[dev]` | `pytest`, `ruff`, `mypy` | development                          |

The Android source needs only the `adb` executable (Android platform-tools) on
your PATH; the `dji` import source needs the external `dji-log` binary (on your
PATH or via `DJI_LOG_BIN`). Neither needs a Python extra.

## Quickstart

Collect from the built-in simulator into a BDF file:

```python
from battfeed import BdfCsvSink, Harvester, create_source

harvester = Harvester()
harvester.register(create_source("simulator"))
sink = BdfCsvSink("LOCAL__DemoCell__20260707_001.bdf.csv", metadata={"operator": "me"})
harvester.collect("simulator", duration_s=10, interval_s=1.0, sink=sink)
sink.close()  # finalises the CSV and writes the .meta.json sidecar
```

Or from the command line (Ctrl-C stops gracefully and finalises the files):

```
battfeed collect --source simulator --duration 10 --interval 1 --institution LOCAL --cell DemoCell
```

Omit `--duration` to collect until Ctrl-C — the mode field collectors run in.
Source constructor options are passed with repeatable `--opt KEY=VALUE` flags
(values are parsed as JSON when possible):

```
battfeed collect --source mc3000 --opt slot=1 --opt transport=ble --cell AA-Bay2
battfeed collect --source android --opt serial=R58M12ABC --interval 5
battfeed collect --source csvtail --opt path=instr.log --opt 'column_map={"V":"voltage_volt","I":"current_ampere"}'
```

`battfeed sources` lists everything available — including sources contributed by
other installed packages — with each source's options, and marks unavailable
ones (e.g. `wmi` off-Windows, `mc3000` without its transport extra).

## What comes out

A conforming BDF CSV with snake_case `{quantity}_{unit}` headers. The required trio
`test_time_second, voltage_volt, current_ampere` always leads the header, followed by
any extra columns in alphabetical order. Files are named
`InstitutionCode__CellName__YYYYMMDD_XXX.bdf.csv` (see `battfeed.sinks.bdf_csv.dataset_filename`).

**Sign convention** (per the BDF specification): positive current charges the test
object, negative current discharges it. Power follows the same sign.

If a source does not stamp its own `test_time_second`, the harvester stamps each sample
with the elapsed collection time.

## Sinks

A **sink** is where collected samples land. Everything above writes to a
`BdfCsvSink`, but battfeed ships four:

- **`BdfCsvSink`** — one BDF `.bdf.csv` file for one test object. The default;
  use it for a single cell, bay, or device. The sidecar is written early
  (marked `"finalized": false`) and finalised on `close()`, so a crash
  mid-collection still leaves valid metadata on disk.
- **`RoutingSink`** — demultiplexes one stream into **one BDF file per
  `(series_id, run_id)`**, with optional time/row rotation. Use it when one
  connection yields many objects or runs (see *Multi-object routing* below).
- **`HttpPushSink`** — POSTs samples as gzipped newline-delimited JSON to any
  HTTP endpoint on a time cadence, stdlib-only. Use it to feed a registry or lab
  server. Delivery is at-least-once: rows are kept on a failed POST and spooled
  to a loadable `.spool.ndjson` if the endpoint stays down.
- **`ParquetSink`** — one Parquet file (plus the same `.meta.json` sidecar) per
  bounded capture, for analysis in pandas/Polars/DuckDB. Needs `battfeed[parquet]`.
  Use it for finite analytical captures, not unbounded telemetry (that is
  `RoutingSink` + BDF).

`BdfCsvSink` **strips** the routing keys (they are never BDF columns); the
Parquet and HTTP sinks **keep** them, as a column and payload field respectively,
so downstream code can group by object and run.

### Multi-object routing

Two optional reserved keys on a sample say *where it belongs* rather than *what
was measured*:

- **`series_id`** — which physical object the sample is from (a car, a pack, a bay);
- **`run_id`** — which test-run segment it belongs to.

A source that emits them must supply its own `test_time_second`, zero-based per
`(series_id, run_id)` — the harvester's shared elapsed-collection clock is wrong
for an object that appears mid-run. `RoutingSink` sanitises raw `series_id`
device serials into safe BDF cell names, opens each object's file lazily on its
first sample, and reports what it wrote via `files_by_series`.

## Importing files

Some sources ingest **complete files** rather than polling a live device — a
folder of exported logs, say. `battfeed import` drains such a source through the
same `DataSource` seam, writing through a `RoutingSink` so the result is one
`.bdf.csv` (plus sidecar) per `(series_id, run_id)`:

```
battfeed import --source dji --opt path=C:/logs/dji --out-dir imported
```

One-shot by default — it stops when the source reports drained and prints
exactly what it wrote (or "nothing to import"); pass `--watch` to keep polling a
folder for new files until Ctrl-C. Import is **at-least-once**: a batch source
records a file as ingested only *after* its rows are safely written, so a crash
mid-import re-imports the file into new segment files rather than losing it.
Dedupe and quarantine are backed by an `ImportLedger` (sha256 content hashes;
permanently-unsupported files are quarantined with a reason and never retried).
Deleting output files does not reset the ledger; `battfeed import --reset-ledger`
does.

## Configuration files

A long invocation — an mc3000 slot and BLE address, a dji folder and keychain
key — can live in a TOML file instead of a wall of `--opt` flags. Pass it with
`--config` on `collect` or `import`:

```toml
[collect]                        # defaults for `battfeed collect`
interval = 2.0
institution = "SINTEF"
cell = "Pack-07"

[import]                         # defaults for `battfeed import`
out_dir = "imported"
watch = true

[source.mc3000]                  # options for `--source mc3000`
slot = 1
transport = "ble"
address = "AA:BB:CC:DD:EE:FF"

[source.dji]                     # options for `--source dji`
path = "C:/logs/dji"
api_key = "${ENV:DJI_API_KEY}"   # expanded from the environment at load time
```

A `[source.<name>]` block matches `--source <name>`; its optional `type` field
selects the registered source (defaulting to the block name, so a block can
alias one source under another name). **Precedence**, highest first:

- source options: `--opt KEY=VALUE` > `[source.<name>]` > the source's own default
- run parameters: the CLI flag > `[collect]`/`[import]` > the built-in default

`--config` uses `tomllib`, standard library on Python 3.11+; on 3.10 install the
`tomli` backport (`pip install tomli`) — no dependency is added to the core.

## Keeping secrets out of sight

Passing a token as `--opt api_key=...` leaks it into shell history and the
process list (`ps` / Task Manager show a process's full command line). Prefer a
config file with **`${ENV:VAR}` expansion**, so the secret lives only in the
environment and never touches the file:

```toml
[source.push]
token = "${ENV:INGEST_TOKEN}"
```

Whatever the entry path, battfeed redacts credentials in two complementary
layers before they can reach a log, an error message, the `battfeed sources`
listing, or a `.meta.json` sidecar:

- **by key name** — a value whose key looks like a credential (`*key*`,
  `*token*`, `*secret*`, `*password*`, `*auth*`, ...) is masked to `***`;
- **by value** — the concrete secret values resolved for a run, plus any
  `scheme://user:pass@host` URL userinfo, are scrubbed even when they hide under
  an innocuous key.

The helpers are public — `battfeed.config.redact_mapping`, `redact_text`,
`is_secret_key` — for source authors who need them.

## Writing your own source

`battfeed.DataSource` is a `typing.Protocol` — the stable seam third-party collectors
implement. No imports from battfeed are needed; any object with `name`, `metadata()` and
`poll()` qualifies:

```python
class MyCyclerSource:
    name = "my-cycler"

    def metadata(self):
        return {"source": self.name, "vendor": "ACME", "channel": 3}

    def poll(self):
        # Return zero or more NEW samples since the last call,
        # keyed by canonical BDF column names.
        reading = my_driver.read_channel(3)
        return [{"voltage_volt": reading.volts, "current_ampere": reading.amps}]
```

Register it with a `Harvester` directly, or expose it to the `battfeed` CLI from your
own package via an entry point:

```toml
[project.entry-points."battfeed.sources"]
my-cycler = "my_pkg.sources:MyCyclerSource"
```

Sources may optionally define `close()` to release hardware handles, and an
`availability()` classmethod to explain why they cannot run here (missing extra,
wrong platform); battfeed uses both when present. See `examples/custom_source.py`
for a runnable version.

**Keep sources simple: raise on trouble.** When the device is unreachable,
`poll()` should raise — the `Harvester` retries with exponential backoff under a
configurable `ErrorPolicy` and only abandons the run (raising `SourceFailure`)
after too many *consecutive* failures. Sources should not implement their own
retry loops; only swallow errors you can genuinely resolve better yourself
(e.g. one bad frame out of several channels). This is what makes multi-day
field collection survive flaky Bluetooth and USB.

**Test it with `check_source`.** `battfeed.testing.check_source` asserts the
essentials every pipeline relies on — the `name`/`metadata()`/`poll()` surface,
strict-JSON metadata and samples, sample shape, and the routing-key discipline —
so a one-liner in your own test suite is a complete contract test:

```python
from battfeed.testing import check_source

def test_my_source_contract():
    check_source(MyCyclerSource(...))  # drive it with a mock or a replay tape, never live hardware
```

**Push-style hardware (BLE, CAN, MQTT).** Devices that *push* readings don't fit
a synchronous `poll()` directly. Subclass `battfeed.StreamingSource`: implement a
blocking `run_reader(emit, should_stop)` loop that calls `emit(sample)` for each
reading, and the base runs it in a background thread and buffers samples for
`poll()` to drain. The buffer is bounded and overflow is counted (never silently
lost); a reader that dies re-raises at the next `poll()` so the harvester's
`ErrorPolicy` owns the retry — no retry loop inside your reader. Because BLE/CAN
tooling is Linux-first and hardware isn't on every desk, the supported workflow
is **replay-first**: record raw frames from one live session into a JSONL *tape*
(`battfeed.testing.ReplayTape` / `TapeRecorder`), commit it, and drive every test
and most development from the tape with `ReplayReader` — which compresses time,
so an hour-long tape replays in milliseconds. See `examples/streaming_source.py`.

## Built-in sources

| Name        | Class                | What it does                                                                 |
| ----------- | -------------------- | ---------------------------------------------------------------------------- |
| `simulator` | `SimulatedCellSource` | Deterministic synthetic CR2032-ish discharge; ideal for demos and tests.    |
| `csvtail`   | `CsvTailSource`       | Tails a growing CSV log; you supply the column map and unit scale factors.  |
| `wmi`       | `WmiBatterySource`    | Polls the local Windows battery via WMI (`battfeed[wmi]`, Windows only).     |
| `mc3000`    | `Mc3000Source`        | SkyRC MC3000 charger/analyzer, one slot per instance, over BLE/USB (or a built-in mock transport for demos). |
| `android`   | `AndroidBatterySource` | Android device battery via `adb` (dumpsys + sysfs); pure stdlib.           |
| `dji`       | `DjiFlightLogSource`  | Import DJI Fly flight-log records (`*.txt`/`*.dat`) as one BDF file per (aircraft+battery, flight), via `battfeed import`. Wraps the external `dji-log` binary. Record files come from untrusted media, so battfeed passes the path safely to `dji-log` and quarantines anything malformed. Decrypting v13+ records calls DJI's keychain API. |

## Non-goals

Keeping battfeed small is the point. It deliberately does **not** do:

- **Vendor-file normalization.** Parsing and harmonising exported Neware / BioLogic /
  Digatron / Basytec / ... files is the job of
  [`batterydf`](https://github.com/battery-data-alliance) (Battery Data Alliance).
  `CsvTailSource` is config-driven on purpose — it will never guess column synonyms.
- **Upload, fleet management, or multi-tenant services.** `HttpPushSink` is a
  generic one-way POST to an endpoint you point it at — a *sink*, not a hosted
  service; battfeed runs no server and manages no fleet or tenants. Publishing
  and sharing belong to registry tooling.
- **Digital-twin or model logic.** State estimation and twin orchestration live in
  [`battwin`](https://github.com/DigiBatt/battwin), which consumes battfeed feeds.

## Development

```
pip install -e ".[dev]"
pytest
```

## Acknowledgements

This project has received support from European Union research and innovation
programs under grant agreement 101103997 (DigiBatt).

## License

Apache-2.0
