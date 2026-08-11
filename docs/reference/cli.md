# CLI reference

Installing battfeed puts a `battfeed` command on your path. Two global options come before the verb: `-v/--verbose` enables INFO-level logging to stderr (data never goes to stdout/stderr, so logs and data cannot mix), and `--version` prints the version.

```console
$ battfeed --version
battfeed 0.5.0
```

## `battfeed sources`

```bash
battfeed sources
```

Lists every available data source — built-ins and sources contributed by other installed packages through the `battfeed.sources` entry-point group — with each source's constructor options and defaults. Sources that cannot run on this machine are listed anyway, marked `[unavailable: <reason>]` with the concrete fix (which extra to install, what is missing from PATH).

## `battfeed discover`

```bash
battfeed discover [--source NAME] [--timeout SECONDS] [--opt KEY=VALUE ...] [--json]
```

Scans for devices the hardware sources can collect from and prints ready-to-paste collect commands. Without `--source`, every discovery-capable source is scanned; with it, just that one (`--opt` requires `--source`). `--timeout` is the budget per source scan (default 6.0 s); `--json` emits `{source: [candidate, ...]}` for scripting.

MC3000 chargers are found by their advertised FFE0 BLE service (a charger connected to another program stops advertising — close that program first). Android candidates are devices connected to adb plus wireless-debugging listeners seen over mDNS (unreliable on Windows; `adb connect ip:port` a phone that is not listed).

When exactly one device is in range, skip the scan-then-paste step: `--opt address=auto` (mc3000) and `--opt serial=auto` (android) resolve the single unambiguous device at start-up and fail with the candidate list otherwise — never a silent guess, so unattended runs stay deterministic.

## `battfeed collect`

```bash
battfeed collect --source NAME [options]
```

Polls a source on an interval and writes a `.bdf.csv` file plus its `.meta.json` sidecar. Ctrl-C stops gracefully and finalises the files.

| Option | Meaning |
|---|---|
| `--source NAME` | source name (required; see `battfeed sources`) |
| `--duration SECONDS` | how long to collect (default: until Ctrl-C) |
| `--interval SECONDS` | polling interval (default: 1.0) |
| `--opt KEY=VALUE` | source constructor option, repeatable; values parsed as JSON when possible; overrides a `[source.<name>]` config block |
| `--out PATH` | output path (default: generated `Institution__Cell__YYYYMMDD_NNN.bdf.csv`; do not fix this under a restarting service — see [unattended operation](../howto/run-unattended.md)) |
| `--institution CODE` | institution code in the generated name (default: `LOCAL`) |
| `--cell NAME` | cell name in the generated name (default: the source name) |
| `--config FILE` | TOML file supplying source options and run parameters ([details](../howto/config-files.md)) |

## `battfeed import`

```bash
battfeed import --source NAME [options]
```

Drains a batch/file-import source into per-`(series, run)` `.bdf.csv` files. One-shot by default (stops when the source reports drained); at-least-once delivery with a content-hash dedupe ledger and quarantine ([details](../howto/import-files.md)).

| Option | Meaning |
|---|---|
| `--source NAME` | source name (required) |
| `--opt KEY=VALUE` | source constructor option, repeatable |
| `--watch` / `--no-watch` | keep polling for new files until Ctrl-C / force one-shot over a config `watch = true` |
| `--interval SECONDS` | idle interval between checks for new files (default: 5.0) |
| `--out-dir DIR` | directory for the output files (default: current directory) |
| `--institution CODE` | institution code in the generated names (default: `LOCAL`) |
| `--reset-ledger` | clear the source's dedupe ledger before importing, re-ingesting everything (deleting output files never resets the ledger) |
| `--config FILE` | TOML config file, as for `collect` |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | duration elapsed, source drained, or interrupted cleanly |
| `1` | the source failed (its error budget was exhausted) |
| `2` | configuration mistake: bad source name or options — restarting will not fix it, so always run a service's exact command interactively once |
