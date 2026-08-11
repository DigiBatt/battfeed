# How battfeed survives flaky hardware

Field collection means multi-day runs over Bluetooth that drops, USB that re-enumerates, phones that wander off, and hosts that crash. battfeed's reliability model is a few mechanisms that compose, all serving one rule: **never lose data quietly.** Whatever fails, the loss is counted, set aside, or spooled — never silent.

## Retry lives in one place

Sources raise when the device is unreachable; the `Harvester` retries with exponential backoff under a configurable `ErrorPolicy` and abandons the run (raising `SourceFailure`, CLI exit 1) only after too many *consecutive* failures. Centralising retry does two things: every source gets production-grade resilience for free, and the failure accounting is uniform — a run that survived 40 transient BLE drops says so in one place instead of hiding it inside a driver.

## Crash-safe files

Rows are flushed after every poll, and the `.meta.json` sidecar is written early with `"finalized": false`, flipping to `true` only on a clean exit. A hard kill therefore costs at most the last poll's worth of data, and the sidecar state tells downstream tooling exactly which pairs are complete. Combined with bounded `--duration` runs under a supervisor, every completed hour is a finished, shippable file — the [unattended-operation recipes](../howto/run-unattended.md) are this design used as intended.

## At-least-once, end to end

Where battfeed hands data to something that can fail, the guarantee is at-least-once, and duplication is preferred to loss:

- **Import**: a batch source records a file as ingested only *after* its rows are safely written, so a crash mid-import re-imports rather than drops; the `ImportLedger` dedupes by sha256 content hash, and permanently-bad files are quarantined with a recorded reason instead of being retried forever.
- **HTTP push**: rows are kept on a failed POST and spooled to disk if the endpoint stays down; a dead server costs latency, never data.
- **Streaming sources**: the push-to-poll buffer is bounded and overflow is *counted*, never silently discarded; a dead reader thread re-raises at the next `poll()` so the error policy sees it.

## Replay-first testing

Hardware sources are developed and tested against recorded *tapes*, not live devices: record raw frames from one live session (`TapeRecorder`), commit the JSONL tape, and drive every test with `ReplayReader`, which compresses time so an hour-long session replays in milliseconds. Tests become deterministic, CI needs no Bluetooth, and a bug report can come with the tape that reproduces it. `check_source` sits on top as the contract test every source — shipped or third-party — is held to.

## Known limits

Honest edges, written down rather than discovered in production: the routing sink holds one open file per active object and does not close idle ones (fine for hundreds of objects, not tens of thousands); the import ledger assumes a single importer process; machine sleep pauses collection with no catch-up on resume, and device-backed sources often need their supervisor restart after it. Details and mitigations live in the [development guide](../project/development.md) and the [unattended-operation page](../howto/run-unattended.md).
