# battfeed implementation plan — agent work packages

This decomposes [ROADMAP.md](ROADMAP.md) into concrete, agent-sized work
packages (WPs). Each WP is one focused unit of work with a **handoff prompt**
that a fresh-context agent can execute without this document's history.

## How to use this document

1. **Prepend the [Common Context](#common-context-prepend-to-every-prompt)
   block to every WP prompt** — agents start with no conversation history.
2. Dispatch **one WP per agent session**. WPs are sized to land as one
   reviewable change.
3. Respect the [dependency graph](#dependency-graph); WPs on independent
   branches may run in parallel.
4. **Gate every WP** before starting its dependents: run the acceptance
   criteria, run a code review pass on the diff, and check the change against
   the Invariants in ROADMAP.md.

---

## Common Context (prepend to every prompt)

```text
CONTEXT — battfeed work package

You are implementing one work package of a planned build-out of `battfeed`,
a Python package at c:\Users\simonc\Documents\Github-local\DigiBatt\gleaned.
battfeed turns live battery data sources into BDF (Battery Data Format)
feeds. READ FIRST, in this order:
  1. ROADMAP.md  (the plan; pay attention to the Invariants section and the
     contract-extension section)
  2. README.md
  3. src/battfeed/protocols.py, src/battfeed/harvester.py,
     src/battfeed/registry.py, src/battfeed/sinks/bdf_csv.py,
     src/battfeed/cli.py
  4. One existing source end-to-end as a style reference:
     src/battfeed/sources/mc3000/ (source + transports + protocol) and its
     tests in tests/.

HARD INVARIANTS (from ROADMAP.md — violating any of these fails the WP):
  I1. battfeed NEVER writes to a device or bus. Read-only must be structural
      (transport-layer allowlists), not conventional.
  I2. No silent data loss: dropped rows / overflows / skipped files are
      counted and surfaced (stats, metadata(), sidecars).
  I3. One seam: everything that produces samples is a DataSource
      (typing.Protocol: name, metadata(), poll(); optional close(),
      availability()). No parallel pipelines.
  I4. Sources raise on trouble; the Harvester owns retry/backoff. No retry
      loops inside sources.
  I5. Any source that emits routing keys (series_id / run_id) supplies its
      own test_time_second, zero-based per (series, run).
  I6. The core stays ZERO-dependency (stdlib only). Every integration
      dependency goes behind an optional extra in pyproject.toml.

CONVENTIONS:
  - Python >= 3.10; ruff (line-length 100) and mypy (py310) must pass:
      python -m ruff check src tests && python -m mypy src
  - Tests: pytest in tests/, no hardware, no network, fake clocks where
    timing matters (see tests/test_harvester.py). Run: python -m pytest
  - Match the existing code's voice: docstring-heavy modules that explain
    *why* (sign conventions, unit conversions, verified-on-hardware notes),
    lazy optional imports with actionable ImportError messages, and an
    availability() classmethod for anything platform/dependency-gated.
  - New sources register in BOTH pyproject.toml
    [project.entry-points."battfeed.sources"] AND registry._BUILTINS, plus a
    row in the README built-in sources table.
  - BDF sign convention: positive current CHARGES the test object.
  - Update CHANGELOG.md under an Unreleased heading. Do NOT bump the version,
    do NOT push, do NOT publish. Commit your work to a local branch named
    wp/<WP-ID>.
  - When done: run pytest + ruff + mypy, then re-read your diff against the
    invariants above and state explicitly which invariants your change
    touches and how it preserves them.
```

---

## Dependency graph

```
WP1.1 (contract + sink hygiene)
  ├─► WP1.2 (RoutingSink + rotation)
  │      ├─► WP1.5 (P1 integration + DoD)
  │      └─► WP2.3, WP4.2, WP4.3  (all routed sources)
  ├─► WP1.3 (import driver + CLI verb + ledger) ─► WP1.4 (DJI source) ─► WP1.5
  └─► WP2.1 (StreamingSource) ─► WP2.2 (replay fixtures) ─► WP2.3 (Victron)
                               └─► WP3.3 (CAN), WP4.5 (MQTT), WP3½.1 (Neware)
WP3.1 (--config + redaction) ─► WP3.2 (SunSpec), WP3.3 (CAN/DBC), WPO.1 (run)
WP4.1 (TokenStore) ─► WP4.2 (Ecoflow) ─► WP4.3 (Tesla)
WP4.4 (push sink + Parquet)  [after WP1.2; coordinates with the proprietary platform]
WPO.2 (CI Linux leg + service recipes)  [any time after WP2.1]
```

Parallelizable from day one: **WP1.1 → {WP1.2, WP1.3, WP2.1}** are three
independent tracks after WP1.1 lands. WP2.1 does not depend on Phase 1 at all
except for WP1.1's protocols documentation.

Sizes: S ≈ half a session, M ≈ one session, L ≈ one session of building plus
one of hardening.

---

## Phase 1 — Routing contract + DJI import

### WP1.1 — Reserved-key contract + BdfCsvSink hygiene (S)

```text
WP1.1 — Reserved routing keys in the sample contract; BdfCsvSink hygiene.

BUILD:
1. In src/battfeed/protocols.py: add
     RESERVED_KEYS: tuple[str, ...] = ("series_id", "run_id")
   and document the routing contract in the module docstring and DataSource
   docstring: series_id identifies WHICH physical object a sample belongs to
   (car / pack / bay), run_id identifies WHICH test-run segment. Both are
   optional per sample, both are str when present, both are STRIPPED before
   any BDF output and are never CSV columns. State invariant I5 (a source
   emitting these keys must supply its own zero-based-per-(series,run)
   test_time_second, because the harvester's shared elapsed-collection stamp
   is wrong for objects that appear mid-run).
2. In src/battfeed/sinks/bdf_csv.py: BdfCsvSink strips RESERVED_KEYS from
   every row before header inference and writing (so a routing-aware source
   wired directly to the plain sink cannot leak them into columns).
3. Early sidecar: BdfCsvSink writes the .meta.json sidecar when the file is
   FIRST OPENED (with "finalized": false and current row count 0), and
   REWRITES it on every N rows or T seconds (pick something cheap, e.g.
   every flush is too often — every 60 s of wall time is fine) and finally on
   close() with "finalized": true. Rationale: a crash mid-collection must
   leave a valid data file WITH metadata on disk (ROADMAP "unbounded
   streams"). Keep close() idempotent.
4. Tests: reserved keys never appear in CSV headers or rows even when
   present in samples; sidecar exists on disk before close and carries
   finalized:false; after close it carries finalized:true and the final row
   count; existing tests still pass unchanged.

OUT OF SCOPE: RoutingSink itself (WP1.2), any source changes.
```

### WP1.2 — RoutingSink with rotation (M)

```text
WP1.2 — RoutingSink: demultiplex one sample stream into one BDF file per
(series, run), with rotation for unbounded streams.

READ ALSO: the "contract extension" section of ROADMAP.md in full.

BUILD (new file src/battfeed/sinks/routing.py, exported from
battfeed.sinks and battfeed):
1. class RoutingSink implementing the Sink protocol. Constructor:
     RoutingSink(directory, *, institution="LOCAL",
                 series_info=None,          # callable(series_id) ->
                                            #   (cell_name, metadata_dict)
                 metadata=None,             # shared base metadata
                 rotate_after_s=None,       # time-based rotation
                 rotate_after_rows=None,    # size-based rotation
                 sink_factory=BdfCsvSink)   # injectable for tests
2. write(rows): group rows by (row.get("series_id"), row.get("run_id"));
   rows WITHOUT series_id go to a single default stream. For each new
   (series, run) pair, open a child BdfCsvSink named via dataset_filename():
   cell_name from series_info(series_id) if provided, else a sanitized
   series_id; the _XXX sequence slot advances per run/segment for the same
   series on the same day. Strip reserved keys before delegating (the child
   sink also strips — defense in depth).
3. SANITIZATION: series_id values are raw device serials — may contain "__"
   (reserved as the BDF filename separator), path separators, characters
   illegal on Windows filenames, or be empty. Write one sanitize function
   with tests; on collision after sanitization, disambiguate
   deterministically.
4. RUN SEMANTICS: a NEW run_id for a known series closes the previous
   segment's child sink and opens a new file. Rotation (rotate_after_s /
   rotate_after_rows, whichever trips first) does the same WITHOUT a run_id
   change — segments are runs for endless streams. Inject a clock callable
   for testability (see Harvester's clock/sleep injection for the pattern).
5. PER-SEGMENT METADATA: each child sink's sidecar metadata = shared base
   metadata + series_info metadata + {"series_id": ..., "run_id": ...,
   "segment": n}. Closing a segment finalizes its sidecar (WP1.1 behavior).
6. close(): closes all children, idempotent. Track and expose per-series
   file lists (property) so callers/CLI can report what was written.
7. Tests: multi-series interleaved samples → correct per-file rows; run_id
   change rotates; time and row rotation rotate (fake clock); sanitization
   table-driven; series appearing mid-stream gets its own file and sidecar;
   default stream works with routing-free samples; nothing from
   RESERVED_KEYS in any CSV.

OUT OF SCOPE: CLI integration (WP1.5), any source.
```

### WP1.3 — Import driver: `battfeed import`, dedupe ledger, quarantine (M)

```text
WP1.3 — Batch-import driver: a CLI verb and state layer for sources that
ingest complete files (folder-watch), keeping importers ordinary DataSources
(invariant I3).

BUILD:
1. src/battfeed/importer.py: a driver function
     run_import(source, sink, *, watch=False, interval_s=5.0, stop=None)
   that calls source.poll() repeatedly and writes batches to the sink.
   One-shot mode: stop when the source reports drained — support an OPTIONAL
   source hook `drained() -> bool`, checked after an empty poll; a source
   without the hook is drained after its first empty poll. Watch mode: poll
   forever at interval_s until the stop event (reuse the Harvester's
   ErrorPolicy for poll failures rather than duplicating backoff logic — if
   reuse is awkward, extract the backoff into a small shared helper instead
   of copying it).
2. src/battfeed/ingest_state.py: an ImportLedger for batch sources —
   JSON-file-backed, records sha256 content hashes of ingested files and a
   quarantine set of permanently-unsupported files with reasons. API:
   seen(path)->bool, record(path), quarantine(path, reason),
   is_quarantined(path). Atomic writes (write-temp-then-replace). The ledger
   file location is the SOURCE's choice (importers pass a path); document
   that deleting output files does not reset the ledger — `--reset-ledger`
   on the CLI verb clears it.
3. CLI: add an `import` subcommand to src/battfeed/cli.py:
     battfeed import --source NAME [--opt K=V ...] [--watch]
       [--interval S] [--out-dir DIR] [--institution CODE] [--reset-ledger]
   It builds the source via create_source() exactly like collect does, and
   writes through a RoutingSink over --out-dir (imported data is inherently
   multi-(series,run)). Ctrl-C in --watch mode stops cleanly and finalizes
   files (mirror collect's signal handling).
4. Tests: driver drains a fake batch source (with and without drained());
   watch mode with a stop event; ledger dedupe/quarantine round-trips and
   survives process restart (re-instantiate from the same file); CLI parse
   + a smoke run against a fake source registered in-process.

OUT OF SCOPE: the DJI source itself (WP1.4).
```

### WP1.4 — DJI flight-log source (L)

```text
WP1.4 — DJI flight-log import source, ported from the tested proprietary
collector. This is a PORT, not a rewrite: reuse working logic, adapt the
output contract.

READ ALSO (the donor code, outside this repo):
  (private donor repo -- exact paths in the untracked coordination notes in the workspace root)
    dji_collector\parser.py     — dji-log CLI wrapper: KEEP LOGIC VERBATIM
    dji_collector\normalize.py  — plausibility gate: KEEP LOGIC VERBATIM
    tests\                      — port the test approach and fixtures
  Note the donor repo is proprietary; you are porting logic into
  Apache-2.0 battfeed with the maintainer's authorization (he owns both).

BUILD (new package src/battfeed/sources/dji/):
1. parser.py — port the proprietary parser.py: classify .txt (parseable app
   record) vs .DAT (unsupported → quarantine) vs unknown; resolve the
   dji-log binary (DJI_LOG_BIN env or PATH; availability() uses
   shutil.which); pass DJI_API_KEY via -a with the key REDACTED in all
   logging; distinguish UnsupportedFormat (never retry) from ParseError
   (retryable). Document loudly: decrypting format-v13+ records makes a
   NETWORK call to DJI's keychain API at parse time — import of modern logs
   is not offline; surface that in the error message when decryption fails.
2. gate.py — port the plausibility gate: drop epoch-zero pre-GPS-lock
   timestamps and corrupt-tail rows (pack voltage vs per-cell sum > 0.5 V;
   per-cell 2.0–4.6 V window; |I| <= 200 A; -40..100 °C; SOC 0..100).
   Keep the drop-reason counters (invariant I2).
3. source.py — class DjiFlightLogSource (name "dji"):
   - Constructor: path (a single record file OR a directory to scan),
     ledger_path=None (defaults beside the data), dji_log_bin=None,
     api_key=None (falls back to DJI_API_KEY), include_gps=False.
   - poll(): process the NEXT un-ingested file (one file per poll keeps
     polls bounded, invariant-I4-friendly): parse via dji-log to a temp CSV
     (use the scratch/temp dir), normalize through the gate, and return ALL
     rows of that flight as one batch. drained() -> True when no
     un-ingested files remain. Uses WP1.3's ImportLedger + quarantine.
   - Output rows (BDF columns): voltage_volt, current_ampere (DJI reports
     positive = draw FROM the pack; BDF positive = charging → NEGATE),
     power_watt (same sign), surface_temperature_celsius, plus per-cell
     cell_N_voltage_volt columns; test_time_second = (row_ts −
     flight_start_ts).total_seconds(), zero-based per flight (invariant I5);
     series_id = "<aircraftSerial>:<batterySerial>" (fallbacks like the
     donor when serials are missing); run_id = "flight-<sha256[:12] of the
     raw file>".
   - metadata(): kind "dji-flight-log", the dji-log binary + version if
     cheaply available, and a notes field covering the sign negation, the
     network-decryption caveat, and the per-cell EXTENSION columns.
   - EXTENSION DECLARATION: per-cell columns are not (yet) canonical BDF.
     Provide the extension column list so the sink metadata can carry
     "extension_columns": ["cell_1_voltage_volt", ...] in sidecars — wire it
     through the metadata() mapping.
   - Flight context (GPS if include_gps, height, motor state, remaining/full
     capacity) goes into a per-flight summary inside metadata()/sidecar —
     NEVER as BDF columns, never silently dropped.
4. Register the source (pyproject entry point + registry._BUILTINS +
   README table). No new runtime dependency → no extra needed; document the
   external dji-log binary requirement like android documents adb.
5. Tests (no dji-log binary, no network, port the donor's approach):
   - a FAKE dji-log executable (tiny Python script the test writes to tmp
     and points DJI_LOG_BIN at) that copies a fixture CSV to the -c path;
   - fixture CSVs with the donor's known cases: healthy rows, epoch-zero
     head, corrupt tail, per-cell/pack disagreement;
   - assert: sign negation; per-flight zero-based test_time_second;
     series_id/run_id values; drop counters; .DAT quarantined once and
     remembered; second poll of the same directory yields nothing (ledger).
```

### WP1.5 — Phase 1 integration + DoD verification (S)

```text
WP1.5 — Wire Phase 1 together and prove the ROADMAP Phase 1 definition of
done.

BUILD / VERIFY:
1. End-to-end test: a tmp directory with 2 fixture "flights" from 2
   different packs (fake dji-log from WP1.4's tests) →
   `battfeed import --source dji --opt path=<dir> --out-dir <out>`
   produces one .bdf.csv + finalized .meta.json PER (pack, flight); headers
   lead with test_time_second, voltage_volt, current_ampere; sidecars carry
   series_id/run_id, extension_columns, and flight context.
2. Idempotency: run the same command again → zero new files, clear "nothing
   to import" reporting.
3. If the batterydf package is installed (battfeed[bdf] extra), validate an
   emitted file via battfeed.sinks.bdf_csv.validate_file and assert the
   report is ok, allowing declared extension columns; mark the test
   skip-if-not-installed.
4. Update README: `import` verb section, dji row in the sources table, a
   short "multi-object routing" paragraph pointing at RoutingSink.
5. Update CHANGELOG under Unreleased summarizing Phase 1.
```

---

## Phase 2 — StreamingSource + Victron SmartShunt

### WP2.1 — StreamingSource base (M)

```text
WP2.1 — StreamingSource: the base that adapts push-style delivery (BLE
notifications, CAN frames, MQTT messages) to the synchronous poll() seam.

BUILD (new file src/battfeed/sources/streaming.py, exported from battfeed):
1. class StreamingSource — a base class (this one IS meant to be
   subclassed; the Protocol seam is unchanged for third parties):
   - Subclasses implement `run_reader(self, emit, should_stop)` — a blocking
     loop that calls emit(sample_dict) for each received reading and returns
     when should_stop() is True. The base runs it in a daemon thread started
     lazily on first poll().
   - Bounded buffer: collections.deque(maxlen=buffer_size) with an explicit
     overflow COUNTER (deque's silent maxlen-drop alone violates invariant
     I2 — count what was lost and expose dropped_total; log a rate-limited
     warning).
   - poll(): drains and returns the buffer. ERROR PROPAGATION (this is the
     core of the design): if the reader thread has died with an exception,
     poll() re-raises that exception (then allows a later poll to restart
     the reader) — so the Harvester's ErrorPolicy owns retry (invariant I4).
     Use a lock or thread-safe handoff for the pending-exception slot.
   - close(): signals should_stop, joins the thread with a timeout,
     idempotent, restartable (a later poll() may reconnect).
   - Surface stats: received_total, dropped_total, reader_restarts —
     include them in a base metadata() contribution subclasses merge in.
2. Tests with a scripted fake reader (no hardware, no sleeps beyond tiny
   joins): emitted samples arrive in order across polls; overflow drops the
   oldest and counts them; a reader that raises → next poll() raises that
   same exception → the poll after that restarts the reader; close() then
   poll() reconnects; concurrent emit-while-draining is safe (hammer test).
3. Document in the module docstring WHY the buffer is bounded and why
   errors surface at poll() — cite invariants I2/I4.
```

### WP2.2 — Record/replay fixture pattern (S)

```text
WP2.2 — Record/replay: make streaming sources testable and developable
without hardware (primary dev machine is Windows; BLE/CAN tooling is
Linux-first, so replay-first is the default workflow).

BUILD:
1. src/battfeed/testing/replay.py (new battfeed.testing subpackage, shipped
   — it is the contract test kit's home): a JSONL "tape" format for raw
   frames: {"t": <seconds-offset>, "data": "<hex>", "meta": {...}} — plus
   ReplayTape.load/save and a ReplayReader that a StreamingSource subclass
   can drive its run_reader from, with time compression (replay a 1-hour
   tape in milliseconds using offsets, no sleeps) — clock-injectable.
2. A recording helper: wrap any live reader callback to tee frames into a
   tape file, so field recordings become fixtures with one flag.
3. Contract test kit v0: src/battfeed/testing/contract.py with
   check_source(source) asserting the DataSource protocol essentials
   (name str; metadata() JSON-serializable; poll() returns list of dicts
   with str keys; reserved-key discipline: if series_id/run_id present then
   test_time_second present — invariant I5). Export it and document it in
   README's "writing your own source" section: third parties run this in
   their own test suites.
4. Tests for the tape round-trip, time compression, and check_source
   against the existing simulator and mc3000 (mock transport) sources.
```

### WP2.3 — Victron SmartShunt BLE source (L)

```text
WP2.3 — Victron SmartShunt over BLE "Instant Readout" advertisements —
first real StreamingSource (Archetype C exemplar).

DECIDE FIRST (and record the decision in the module docstring): reuse the
`victron-ble` library (MIT) vs. a minimal in-tree decoder. Inspect the
library; prefer REUSE behind an extra if its API and maintenance look sane;
an in-tree decoder is acceptable only if the library is a poor fit — justify
either way. Either path needs `bleak` for scanning.

BUILD (src/battfeed/sources/victron/):
1. Subclass StreamingSource; run_reader scans BLE advertisements for the
   configured device address and decodes SmartShunt instant-readout data.
   SETUP HONESTY: adverts are AES-encrypted; the per-device key comes from
   the VictronConnect app. Constructor: address (required), key (required;
   also VICTRON_BLE_KEY env), plus buffer options passed through.
2. Output BDF columns: voltage_volt, current_ampere (check and document the
   shunt's sign vs BDF: positive must mean CHARGING the battery),
   power_watt; SOC and consumed-Ah handling: map SOC to the vocabulary
   agreed in P1's extension mechanism (declare extension columns exactly
   like WP1.4 did). No series_id needed (one shunt = one object) — the
   plain single-object path with harvester-stamped time is correct here.
3. availability(): needs bleak (extra: battfeed[victron]); platform notes
   for Windows scanning quirks in the docstring.
4. Replay-first tests (WP2.2 tapes): commit at least one real recorded
   tape (anonymize the MAC/key), assert decoded values against known-good
   readings; error path: wrong key → clear actionable error surfaced
   through poll() (WP2.1 propagation); NO live-hardware tests in CI.
5. Rotation exercised: an integration test drives Victron-over-replay
   through Harvester + RoutingSink? No — simpler and honest: drive
   simulator/replay samples through RoutingSink with rotate_after_rows to
   prove the P2 DoD's rotation claim in one test; long-duration hardware
   verification is a manual runbook item — write that runbook stub in
   docs/.
6. README + CHANGELOG + entry point + extras registration.
```

---

## Phase 3 — Industrial backbone

### WP3.1 — `--config` file + credential hygiene (M)

```text
WP3.1 — TOML config for sources/sinks and credential hygiene (pulled
forward from Phase 4 because P3 sources already carry credential-adjacent
config, and --opt secrets leak into shell history/process lists).

BUILD:
1. src/battfeed/config.py: load a TOML file (tomllib, stdlib) of the shape:
     [collect]            # defaults: interval, out_dir, institution
     [source.<name>]      # constructor kwargs for create_source(<type>)
       type = "mc3000"
       ...kwargs...
     [sink]               # bdf-csv | routing + its kwargs
   Precedence: CLI flag > env var > config file > default. ENV EXPANSION:
   any string value "${ENV:VAR_NAME}" resolves from the environment at load
   time — this is the sanctioned way to keep secrets out of files.
2. CLI: --config PATH on collect and import; a config may define multiple
   [source.X] blocks but collect/import still target one --source NAME
   (the multi-source supervisor is WPO.1, not this WP).
3. REDACTION: a shared redact() helper; any config/opt key matching
   (key|token|secret|password|api_key) is masked in logs, error messages,
   and the sources listing. Audit existing logging (cli.py, sources) and
   apply.
4. Tests: precedence matrix, env expansion, redaction (assert a secret
   value NEVER appears in captured logs), malformed-config errors are
   actionable.
```

### WP3.2 — SunSpec Modbus source (L)

```text
WP3.2 — SunSpec Modbus source (Archetype E), read-only by construction.

SCOPE HONESTY (from ROADMAP): standardized battery models (802/803) have
spotty adoption; the generic register-map mode is what covers the long
tail. Ship BOTH: sunspec autodiscovery AND a config-driven register mode.

BUILD (src/battfeed/sources/modbus/):
1. transport.py: a thin Modbus client wrapper over pymodbus (extra:
   battfeed[modbus]; PIN pymodbus to a major version — its majors break
   APIs) exposing ONLY read operations (read_holding_registers /
   read_input_registers). Invariant I1 is structural: no write method
   exists on the wrapper, and a test asserts the wrapper's public surface
   contains no write capability.
2. sunspec.py: SunSpec model discovery (scan for the 0x53756e53 "SunS"
   marker at the well-known base addresses, walk the model chain), decode
   the common model for identity/metadata() and battery models 802/803 for
   samples when present.
3. source.py: ModbusBatterySource (name "modbus"): mode="sunspec"
   (autodiscover) or mode="map" with a register map from --config
   (address, count, dtype, scale, bdf_column per entry — csvtail's
   config-driven philosophy: battfeed never guesses maps). Polling
   (Archetype E is pull — no StreamingSource needed). TCP first; RTU/serial
   behind the same wrapper if cheap.
4. Tests against a FAKE Modbus server (pymodbus has a test server, or fake
   the client wrapper): sunspec chain walk on a synthetic register image;
   map mode decode/scale/sign; read-only surface test; availability().
5. Registration, README, CHANGELOG. Example config in examples/.
```

### WP3.3 — CAN / DBC source (L)

```text
WP3.3 — CAN bus source with DBC signal decoding (Archetype D),
LISTEN-ONLY by construction.

BUILD (src/battfeed/sources/can/):
1. Subclass StreamingSource (WP2.1). Reader: python-can Bus (+ cantools
   for .dbc decode), extras battfeed[can]. INVARIANT I1 IS THE HEADLINE: a
   transmit on a live vehicle/pack bus is a physical-safety hazard —
   request listen-only/silent mode from the interface where supported
   (receive_own_messages=False, listen-only bus config per backend),
   NEVER call send(); wrap the bus so no transmit method is reachable, and
   test that the wrapper surface has no send.
2. Config-driven (WP3.1): dbc file path, channel/interface/bitrate, and a
   signal map {DBC signal name -> BDF column, scale, sign} — battfeed
   never guesses signal semantics; the map is explicit. Multi-pack buses:
   an optional message-ID- or signal-based series_id rule (then invariant
   I5: derive test_time_second from CAN frame timestamps zero-based per
   series/run).
3. contrib/dbc/README.md: the curated-map story from ROADMAP — the core is
   config-only; curated per-BMS maps (Orion, REC, Batrium, …) accumulate
   in contrib/ via community contribution. Seed it with the README and one
   example config, not with maps we cannot verify.
4. Tests: replay-first — a recorded/synthetic tape of CAN frames (WP2.2
   tape with frame bytes; or python-can's virtual bus on any OS) decoded
   through a test .dbc fixture; overflow counting; no-send surface test.
   Note: socketcan does not exist on Windows — everything must run on the
   virtual/replay path; document that live-bus verification is
   Linux + hardware and provide a runbook stub in docs/.
```

---

## Phase 3½ — Lab cyclers (opportunistic — dispatch when instrument access exists)

### WP3½.1 — Neware BTS live source (L, blocked on lab access)

```text
WP3½.1 — Neware BTS server live source (Archetype I — live cycler
telemetry; the highest-value scientific source for DigiBatt/battwin).

PRECONDITION (verify before building): network access to a Neware BTS
server (BTS 7/8 middle-machine) in a lab, and its protocol variant
identified. Community implementations exist (e.g. the `neware` /
NewareNDA-adjacent live-API projects) — SURVEY FIRST and prefer wrapping a
maintained client behind an extra over reimplementing a proprietary
protocol from scratch. If no maintained client fits, implement the minimal
read-only query subset observed on the wire, with the mc3000 approach:
protocol.py (meaning) separate from transport (bytes), read-only
allowlist (invariant I1).

BUILD: multi-channel → RoutingSink: one channel = one series_id; the
cycler's step/schedule boundaries = run_id. Cycler data is the one case
where the DEVICE's own test_time is authoritative — use it (invariant I5).
Columns: the full BDF vocabulary applies (voltage, current, capacity,
energy, step index as extension if non-canonical). Replay tests from
recorded traffic (WP2.2). Registration/README/CHANGELOG as usual.
```

---

## Phase 4 — Cloud + feed

### WP4.1 — TokenStore + OAuth machinery (M)

```text
WP4.1 — Secrets/token layer for cloud sources.

BUILD (src/battfeed/auth.py):
1. TokenStore: get/set/delete named secrets with backends tried in order:
   environment (BATTFEED_SECRET_<NAME>), a secrets TOML (path from config;
   warn once that it is plaintext; 0600 perms where the OS supports it),
   and OPTIONAL keyring (extra: battfeed[keyring]) — never a hard
   dependency (invariant I6).
2. OAuthSession helper: given token_url + client credentials + refresh
   token, provide get_access_token() with expiry-aware refresh, single
   retry on 401, and persistence of rotated refresh tokens back to the
   TokenStore. stdlib urllib only in core (invariant I6) — cloud sources
   that need richer HTTP can bring a lib behind their own extra, but the
   refresh helper itself stays stdlib.
3. Redaction (WP3.1) applies to everything here; tokens never in logs or
   errors. Tests with a fake token endpoint (http.server in-thread or a
   fake transport): refresh on expiry, 401-retry-once, rotation persisted,
   keyring backend mocked.
```

### WP4.2 — Ecoflow source (M)

```text
WP4.2 — Ecoflow cloud source (Archetype G exemplar).

VERIFY FIRST (do not skip): EcoFlow IoT Open Platform access requires a
developer-program application. Confirm the maintainer has approved
credentials and which device SKUs/regions the account covers; confirm the
current API shape (REST quota endpoints + MQTT option) and the HMAC
request-signing scheme from the official docs AT BUILD TIME. If access is
not approved yet, STOP and report — do not build against guessed APIs.

BUILD (src/battfeed/sources/ecoflow/): poll the device-quota endpoint(s);
one authenticated account → N devices → series_id per device serial
(RoutingSink fan-out; rotation provides run segmentation — invariant I5:
supply per-(series,segment) test_time_second from response timestamps).
Map to BDF columns with signs verified against a real device
charge/discharge; SOC via the extension mechanism. Credentials via
TokenStore/WP3.1 config (access key + secret + HMAC signing helper).
Rate-limit respectfully: honor documented limits in the default interval;
back off on 429 by raising (Harvester owns retry — invariant I4). Tests
against recorded/fake HTTP responses; no live API in CI.
```

### WP4.3 — Tesla Fleet API source (L)

```text
WP4.3 — Tesla Fleet API source (Archetype G, hard mode).

VERIFY FIRST (do not skip): Fleet API terms move — confirm at build time:
developer registration status, regional endpoints, virtual-key pairing
requirement, per-call billing tiers, and whether the Fleet Telemetry
streaming service or polled vehicle_data is the sane path for battery
fields at hobby scale. Report findings + a cost estimate BEFORE
implementing; get maintainer sign-off on polled-vs-streaming and the
default interval (billing!).

BUILD (src/battfeed/sources/tesla/): OAuth via WP4.1 (authorization-code
flow docs + refresh handled by OAuthSession); account → N vehicles →
series_id per VIN; battery fields (SOC, charge state, voltage/power where
exposed) → BDF columns + extensions; wake semantics: DO NOT wake sleeping
vehicles by default (it costs battery and money) — a sleeping vehicle
yields no samples, documented. Respect rate limits by design; 429/402 →
raise with actionable messages. Recorded-response tests only.
```

### WP4.4 — Push sink (the proprietary platform ingest) + Parquet sink (L)

```text
WP4.4 — Feed sinks: HTTP-ingest push sink and a Parquet file sink.

COORDINATION FIRST: the BDF ↔ telemetry_record decision (ROADMAP Phase 4).
Read the platform contract at
  the proprietary ingest contract telemetry_record.schema.json v1.0.0 (path in the untracked coordination notes)
and the existing uploader pattern in
  the proprietary uploader module (path in the untracked coordination notes; gzipped JSONL
batches, idempotent by content hash, local buffer + retry when offline).
Propose to the maintainer: translate battfeed-side (a
telemetry_record-emitting sink) vs. platform accepts BDF. Default
assumption if unanswered: translate battfeed-side.

BUILD:
1. src/battfeed/sinks/push.py: PushSink implementing Sink — buffers
   locally (JSONL spool), uploads gzipped batches with retry/backoff,
   NEVER loses data when the platform is down (spool persists; invariant
   I2), credentials via TokenStore. Column translation BDF →
   telemetry_record per the coordination decision (test_time_second +
   sidecar start time → absolute ts; series_id → stream_key; run_id →
   extra.run_uid).
2. src/battfeed/sinks/parquet.py: ParquetSink behind battfeed[parquet]
   (pyarrow); same column semantics as BdfCsvSink including sidecar
   parity.
3. Tests: spool/retry/idempotency against a fake HTTP ingest server;
   translation round-trip; parquet read-back equals rows written.
```

### WP4.5 — MQTT / Home Assistant bridge source (M)

```text
WP4.5 — MQTT bridge source (Archetype F) — strategically outsized: it
inherits the Home Assistant / ESPHome / Venus OS ecosystems for the cost
of one source.

BUILD (src/battfeed/sources/mqtt/): subclass StreamingSource; paho-mqtt
behind battfeed[mqtt]. Config-driven topic map (WP3.1): topic pattern →
{bdf_column, scale, json_path?, series_id_from_topic?} — like csvtail,
battfeed never guesses topic semantics, but SHIP two documented presets in
examples/: a Home Assistant statestream mapping and a Venus OS dbus-mqtt
mapping. series_id from topic wildcards → RoutingSink fan-out (invariant
I5: stamp test_time_second from message receipt against a per-series
zero). Reconnect by raising (invariant I4). Tests with an in-process fake
broker or by driving the message callback directly + replay tapes.
```

---

## Operations workstream

### WPO.1 — `battfeed run`: single-host multi-source supervisor (M)

```text
WPO.1 — `battfeed run --config battfeed.toml`: collect from MULTIPLE
sources concurrently on one host (a real site has a shunt + an inverter +
an EV; today that is N processes and no supervisor).

BUILD: for each [source.X] block in the config (WP3.1), run a
Harvester.collect loop in its own thread with its own sink (per-source
sink config; RoutingSink default), one shared stop event, Ctrl-C /
SIGTERM → graceful stop → ALL sinks finalized. A source that exhausts its
ErrorPolicy (SourceFailure) is logged and RESTARTED after a cool-down
(configurable, default 60 s) rather than killing the whole run — the
supervisor's whole point is unattended survival; count restarts and
report. Periodic one-line status logging (per source: samples, errors,
last poll). Single host only — fleet management stays out of scope.
Tests: two fake sources (one healthy, one that dies) with fake clocks —
assert isolation, restart, graceful shutdown, all files finalized.
```

### WPO.2 — CI Linux leg + service recipes (S)

```text
WPO.2 — Operations hardening.

BUILD:
1. .github/workflows/ci.yml: add a Linux job (and keep Windows) running
   pytest + ruff + mypy across supported Pythons; extras installed
   per-job so optional-dependency skips are exercised both ways (a job
   WITH extras and a job WITHOUT — the without-job catches accidental
   hard imports, invariant I6).
2. docs/running-unattended.md: Windows Task Scheduler recipe (port the
   pattern from the proprietary DJI README §Runbook), systemd unit example,
   machine-sleep caveats, log rotation advice, and where import ledgers /
   spool files live.
```

---

## Suggested dispatch order

| Wave | WPs | Notes |
|---|---|---|
| 1 | WP1.1 | Everything hangs off the contract. Review this one hardest. |
| 2 | WP1.2 ∥ WP1.3 ∥ WP2.1 | Three independent tracks. |
| 3 | WP1.4 ∥ WP2.2 | DJI port; replay kit. |
| 4 | WP1.5 → **release 0.5.0** | Phase 1 DoD gate. |
| 5 | WP2.3 ∥ WP3.1 | Victron; config+redaction. |
| 6 | WP3.2 ∥ WP3.3 ∥ WPO.1 → **release 0.6.0** | Industrial backbone. |
| 7 | WP4.1 → WP4.2 → WP4.3, WP4.4 ∥ WP4.5, WPO.2 → **1.0** | Cloud + feed. |
| any | WP3½.1 | The moment lab access exists. |

Release points are maintainer decisions; agents never publish (see Common
Context).
