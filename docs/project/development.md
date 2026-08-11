# Working on battfeed

This is a guide for anyone picking up battfeed to keep building or testing it.
It explains how to set up, how the code is put together, the rules the design
follows, and what is left to do.

## What battfeed is

battfeed reads battery data from a live source (a charger, a phone, a laptop
battery, a log file) and writes it out as BDF — the Battery Data Format, a
simple CSV with standard column names. That is the whole job: get data out of
a device and into a clean, shared file format. It does not clean up messy
vendor files (that is `batterydf`), and it does not run digital twins (that is
`battwin`).

## Getting set up

You need Python 3.10 or newer. On Windows, make the virtual environment with
`py -3` (plain `python` may not be on your path).

```
py -3 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev,parquet]"
```

The core has no third-party dependencies. The extras pull in optional things:
`mc3000-ble`/`mc3000-usb` for the charger, `parquet` for the Parquet sink,
`bdf` to double-check output against the reference format checker, and `dev`
for the test tools.

## Running the checks

Every change should pass all four of these before it is committed:

```
ruff check .          # style and common mistakes
ruff format --check .  # formatting
mypy src               # type checking
pytest -q              # the test suite (currently 424 tests)
```

There is a fifth check that only matters at release time: build the package
and install the built file (not the editable copy) into a fresh, empty
environment, then run `battfeed sources` and a short collect. This catches
packaging mistakes that the normal tests cannot see, because the tests run
against your working copy, not the packaged file.

## How the code is organized

Everything lives under `src/battfeed/`:

- `protocols.py` — the two small contracts everything else plugs into: a
  `DataSource` (something that produces samples) and a `Sink` (something that
  writes them). These use Python's structural typing, so other people can
  write their own sources and sinks without importing battfeed at all.
- `harvester.py` — the loop that polls a source and hands samples to a sink,
  on a timer, with retry-and-back-off when a device is flaky.
- `sources/` — the built-in sources: `simulator`, `csvtail`, `wmi`,
  `mc3000`, `android`, `dji`, plus `streaming.py`, a base class for
  push-style hardware.
- `sinks/` — where data goes: a BDF CSV file, a routing sink that splits one
  stream into one file per battery and test run, an HTTP push sink, and a
  Parquet sink.
- `importer.py` and `ingest_state.py` — the `battfeed import` command, for
  draining folders of logged files, and the record it keeps of what it has
  already imported.
- `config.py` — reading options from a TOML file and keeping secrets out of
  logs and files.
- `cli.py` — the command-line tool.
- `testing/` — tools we ship so other people can test their own sources:
  a source checker and a record/replay system.

## The rules the design follows

A handful of rules hold the whole thing together. Keep to them:

1. **BDF is the only output.** Everything ends up as a BDF file with the same
   column names and the same file-naming pattern.
2. **Never lose data quietly.** If something fails, it is counted, set aside
   ("quarantined"), or saved for a retry — never dropped without a trace.
3. **Importers are just sources.** Reading files uses the same `DataSource`
   contract as reading a live device.
4. **The loop handles flaky devices, not the source.** A source just raises
   an error when the device is unreachable; the harvester decides when to
   retry and when to give up. Sources should not write their own retry loops.
5. **A source that produces many objects supplies its own timeline.** When
   one connection covers several batteries or several test runs, each one
   needs its own time column starting at zero.
6. **The core stays dependency-free.** Optional libraries are only imported
   when the source that needs them is actually used.

## How we test new work

The pattern that built this package: one person (or agent) writes a feature,
then a second, independent person tries hard to break it — with real scripts,
not just opinions — before it is accepted. Findings get fixed and the breaking
scripts are re-run to prove the fix. It is worth continuing this for anything
that touches data safety, hardware, or the file format.

There is also a source checker in `battfeed.testing.check_source`. Any new
source should pass it, and it is the first thing to reach for when reviewing
someone else's source.

## What is built and what is next

Built: the collector core; the six sources above; all four sinks; the import
command with its dedupe record and quarantine; the streaming base and testing
kit; config files and secret handling; and the CI and service-install docs.

Next, roughly in order of the roadmap (`ROADMAP.md` has the detail):

- More device sources — a Victron battery monitor over Bluetooth, solar
  inverters over Modbus, vehicle data over CAN, lab cyclers. Most of these
  need the actual hardware to build and test.
- A supervisor command (`battfeed run`) that runs several sources at once on
  one machine and restarts them if they fall over.
- Sources that need login tokens (some cloud batteries), once the token
  handling is built.

## Known rough edges

These are known and written down in the code; none of them bite at normal
scale, but fix them before pushing battfeed at large fleets:

- The routing sink keeps one open file per active battery and does not close
  idle ones, so tens of thousands of batteries at once would run out of file
  handles. It also does not time-rotate a battery that has gone quiet.
- The import record assumes one importer runs at a time. Two importers
  sharing one record file can overwrite each other's updates.

## Releasing

The package version and the file-format version are separate on purpose (a new
release does not mean a new BDF format). To cut a release: bump the version in
`pyproject.toml`, move the `[Unreleased]` notes in `CHANGELOG.md` under the new
version, build, check the built files, and only then upload. Delete any old
build files first so you do not upload a stale one by mistake.
