# battfeed roadmap

**Goal: make battfeed the universal layer for bringing battery data into the
digital environment — able to harvest from any known battery data source and
feed it, in BDF (Battery Data Format), to various sinks.** battfeed is the
acquisition layer of an open battery-data stack and foundational
infrastructure for [`battwin`](https://github.com/DigiBatt/battwin) (digital
twins) and the proprietary platform.

The design principle throughout: *if it produces battery readings — a live
stream, a register map, a cloud API, or a log file — battfeed should be able
to connect to it and emit conforming BDF.*

This plan has been through an adversarial review; the findings are folded in
below rather than kept in a separate list. Sections marked **⚠ risk** carry
known external dependencies or oversold-claim corrections.

**Execution:** this roadmap is decomposed into agent-sized work packages with
ready-to-dispatch handoff prompts in [IMPLEMENTATION.md](IMPLEMENTATION.md).

---

## Thesis

Three of the four originally-requested first sources (`wmi`, `android`,
`mc3000`) are **done**, and the architecture under them is right and worth
preserving:

- **A stable structural seam.** [`DataSource`](src/battfeed/protocols.py) and
  [`Sink`](src/battfeed/protocols.py) are `typing.Protocol`s — third parties
  implement `name` / `metadata()` / `poll()` (optionally `close()`,
  `availability()`) with **no import of or inheritance from battfeed**.
- **A resilient poll loop.** The [`Harvester`](src/battfeed/harvester.py) owns
  the clock and all retry: `poll()` raises on trouble, `ErrorPolicy` retries
  with exponential backoff and abandons a run only after too many
  *consecutive* failures. Sources never implement retry loops.
- **Entry-point plugin discovery.** The [`registry`](src/battfeed/registry.py)
  finds sources via the `battfeed.sources` entry-point group; battfeed
  registers its own built-ins through it.
- **A proven transport abstraction.** `mc3000` separates protocol meaning
  from byte movement across `ble` / `usb` / `mock` transports, with a
  read-only allowlist and a hardware-free mock.
- **A zero-dependency core**, every integration behind an optional extra.

**The gap to "connect to anything" is not more sources — it is that the
sample contract cannot yet express four things every large integration
needs:** many objects per connection, run boundaries, *unbounded* streams,
and push-style delivery. Fix the contract once and every source afterwards is
the repetition of an established pattern.

---

## Invariants

Non-negotiable properties every phase preserves. New ones added by review are
marked ●new.

1. **battfeed never writes to a device or bus.** ●new — `mc3000` set the
   precedent (read-only opcode allowlist enforced at the transport layer).
   This extends as a hard requirement: **CAN interfaces open in listen-only
   mode** wherever hardware supports it, **Modbus is restricted to read
   function codes** at the transport layer. A defect that transmits on a live
   vehicle or pack bus is a physical-safety hazard, not a data bug; the
   guarantee must be structural, not conventional.
2. **No silent data loss.** Dropped rows, buffer overflows, and skipped files
   are counted and surfaced (stats, `metadata()`, sidecars) — never silently
   discarded.
3. **One seam.** Everything that produces samples is a `DataSource` —
   including batch importers (see Phase 1). No parallel pipelines.
4. **Sources raise; the harvester owns retry.** No retry loops in sources.
5. **Timebase ownership** ●new — any source that emits routing keys
   (`series_id`/`run_id`) **must supply its own `test_time_second`**,
   zero-based per (series, run). The harvester's shared elapsed-collection
   stamp is only valid for single-object sources (a series that comes online
   two hours into a run must not start its file at t = 7200).
6. **Zero-dependency core; extras per integration**; the README
   [non-goals](README.md#non-goals) stand (no vendor-file normalization —
   that is `batterydf`; no fleet management or twin logic).

---

## The contract extension (the real design work)

BDF's invariant is **one test object, one monotonic timebase, per file**. The
world violates it in *three* directions: one connection yields many objects
(a Tesla account → N cars, a drone → N packs), one object yields many runs
(a pack flies many flights, each restarting its clock), and — the case a
lab-test format never had to face — **many sources never end at all** (a
shunt or a car streams forever).

So the `Sample` contract gains **two reserved routing keys**, stripped before
any BDF output:

- **`series_id`** — *which physical object* (which car, which pack, which bay).
- **`run_id`** — *which test/run segment*. Without it, routing on `series_id`
  alone merges e.g. multiple flights of one pack into a file with a
  non-monotonic `test_time_second` — invalid BDF.

A **`RoutingSink`** demultiplexes one stream into one BDF file per
**(series, run)**, reusing the `_XXX` sequence slot of
[`dataset_filename`](src/battfeed/sinks/bdf_csv.py).

**Unbounded streams (●new, from review):** continuous sources have no natural
`run_id`, and today `BdfCsvSink` writes its `.meta.json` sidecar **only on
`close()`** — a months-long feed would have a growing CSV and no metadata on
disk, all of it lost on a crash. Two contract-level fixes, designed in
Phase 1 and exercised in Phase 2:

- **A rotation policy on `RoutingSink`** (time- and/or size-based): each
  rotation closes the current segment as a finished (series, run-segment)
  file and opens the next. Segments *are* runs for endless streams.
- **Sidecars are written early and rewritten on close**, so a crash leaves a
  valid data file *with* metadata, marked unfinalised.

This is the honest resolution of the format/mission tension: BDF models
bounded tests; battfeed collects unbounded telemetry; rotation makes the
unbounded case a sequence of bounded, valid files. The *native* answer for
continuous telemetry is the Phase 4 push-sink feed — files are the interim
interchange, not the end state.

With the contract come three specified pieces:

1. **`series_info(series_id) -> (cell_name, metadata)` hook** on
   `RoutingSink`, so lazily-discovered objects get proper filenames and their
   own sidecar content. Filename derivation sanitizes raw ids (serials can
   contain `__` — reserved by the BDF filename convention — and
   Windows-illegal characters).
2. **Reserved-key documentation** in `protocols.py`: `series_id` / `run_id`
   are part of the sample contract, never BDF columns.
3. **Reserved-key hygiene in `BdfCsvSink`**: the plain sink strips reserved
   keys, so a routing-aware source wired to a non-routing sink cannot leak
   them into CSV columns via header inference.

Decisions on record (2026-07-11):

- **Fan-out = RoutingSink per object** (not one source instance per object).
- **Files now; push sinks in Phase 4.**
- **DJI = flight-log parsing** (batch import), reusing the tested proprietary
  collector — not the Cloud API, not the mobile SDK.

---

## Integration archetypes

Every target maps to one of these shapes; building one exemplar of an
archetype pays for the *plumbing* of its category. (⚠ Honesty note from
review: for Archetype C the plumbing is the cheap part — each BLE BMS is its
own reverse-engineered, firmware-variant protocol dialect. The exemplar
amortizes `StreamingSource`, not the per-device protocol work. The leverage
play there is Archetype F: an MQTT / Home Assistant **bridge source**
inherits hundreds of already-maintained device integrations for the cost of
one source.)

| | Archetype | Mechanism | Targets | Enabler needed |
|---|---|---|---|---|
| A | Host battery | poll, stdlib | `wmi` ✅, `android` ✅, Linux sysfs, macOS IOKit | — |
| B | Instrument transport | request/reply, 1 device | `mc3000` ✅, other chargers/analyzers | — |
| C | BLE monitor | streaming notifications/adverts | **Victron SmartShunt**, JBD/Daly/JK BMS, Renogy | `StreamingSource` |
| D | CAN / DBC | streaming frames, **listen-only** | EV & industrial BMS packs | `StreamingSource` + `python-can`/`cantools` |
| E | Modbus registers | poll, **read-only** | **SunSpec Modbus**, Victron GX, inverters | `pymodbus` (pinned) |
| F | MQTT / broker bridge | streaming subscribe | Venus OS, ESPHome, **Home Assistant bridge** | `StreamingSource` |
| G | Cloud REST / OAuth | poll, multi-object | **Ecoflow**, **Tesla**, Victron VRM, Enphase | routing + `TokenStore` |
| H | Log import | batch, folder-watch | **DJI flight logs**; cousin of `csvtail` | routing + import state |
| I | Lab cyclers, live ●new | vendor TCP/server protocols | **Neware BTS server**, Arbin, Biologic, Maccor | `StreamingSource` or poll |

**Archetype I is not optional garnish.** For battfeed's primary scientific
constituency (DigiBatt, battwin), live cycler feeds are the highest-value
battery data there is — more central to the mission than any consumer cloud
API. It was absent from the first draft of this plan; it is now explicitly
**in scope, sequenced opportunistically by instrument access** (see
Phase 3½). *Live* cycler streaming is battfeed's job; exported-file
normalization remains `batterydf`'s. Known further gaps, named rather than
implied-covered: **OCPP** (EV charging infrastructure) and **Bluetooth
Classic SPP** (many older BMS units are BT-classic, not BLE) — both are
future archetype extensions, not covered by anything below.

---

## Phases

Each phase ships one enabler plus at least one requested source, end-to-end,
with a checkable definition of done.

### Phase 1 — Routing contract + DJI import

- **`RoutingSink`** on the (series, run) contract: `series_info` hook,
  filename sanitization, reserved-key hygiene, **rotation policy** (designed
  here, exercised in Phase 2), **early-written sidecars**.
- **`battfeed import` CLI verb** (one-shot + `--watch`). Per invariant 3,
  importers **remain `DataSource`s**: `import` is a thin driver that calls
  `poll()` to exhaustion under the same `ErrorPolicy` — one seam, one
  contract test kit, no parallel pipeline.
- **Import state**: a content-hash (sha256) dedupe ledger and a quarantine
  for unsupported files (a re-plugged SD card must not re-ingest; a corrupt
  `.DAT` is remembered, not retried). Ledger location and its relationship to
  deleted output files documented explicitly.
- **DJI flight-log source**, ported from the tested proprietary collector
  (see [DJI reuse](#dji-reuse-detail)): `series_id` = pack serial, `run_id` =
  flight uid (content hash), per-flight `test_time_second` supplied by the
  source (invariant 5).
- **Per-cell vocabulary, with a fallback** ●new: DJI logs carry per-cell
  voltages and the ported plausibility gate depends on them, so naming is a
  P1 decision — but coordination with the BDF spec / BattINFO is an external
  standards process that must **not** sit on the critical path. Rule: pursue
  canonical adoption of `cell_N_voltage_volt`; until adopted, emit per-cell
  values under a **declared extension namespace** documented in the sidecar
  (or demote to sidecar-only). The DoD reads "validation green *with
  extensions declared*", so P1 ships regardless of the standards timeline.
- **⚠ dji-log dependency, eyes open** ●new: decrypting format-v13+ records
  makes a **network call to DJI's keychain API at parse time** — import of
  modern logs is *not* offline, fails air-gapped, and lives at DJI's
  pleasure. The binary is a single-maintainer project with three-OS
  distribution friction. Document the failure mode; surface it through
  `availability()` and clear errors. **Flight context (GPS, height, motor
  state) is routed to the sidecar deliberately** — not silently dropped (a
  regression vs. the proprietary implementation otherwise).

*Done when: a directory of Mavic logs → N per-pack-per-flight `.bdf.csv`
files with sidecars, `batterydf` validation green with extensions declared,
and a second run imports nothing.*

### Phase 2 — `StreamingSource` keystone + Victron SmartShunt

- **`StreamingSource` base**: background reader (thread or asyncio) →
  **bounded** buffer → drained by `poll()`. The two requirements that *are*
  the design: a dead reader **re-raises on the next `poll()`** (invariant 4
  survives); buffer overflow is **counted and surfaced** (invariant 2).
- **Victron SmartShunt over BLE** (Archetype C) as the exemplar. Setup
  honesty: "Instant Readout" advertisements are **AES-encrypted**; the
  per-device key exported from VictronConnect is a documented setup step.
  Evaluate reusing the existing `victron-ble` library (MIT) vs. a minimal
  in-tree decoder — decide on inspection, don't default to rewrite.
  ⚠ `bleak` passive advertisement scanning has known Windows quirks; expect
  replay-first development (see Operations).
- **Rotation exercised**: a multi-day SmartShunt run produces a sequence of
  finished, valid (series, segment) files with sidecars — no unbounded file.
- **Record/replay fixture pattern** for hardware-free streaming tests.

*Done when: "if it streams, battfeed connects" is demonstrably true over
BLE; a multi-day run yields rotated, validated files; the test suite is
replay-driven with zero hardware in CI.*

### Phase 3 — Industrial backbone

- **SunSpec Modbus** (Archetype E, `pymodbus` — **pinned**; its majors break
  APIs). ⚠ Scope honestly: standardized *battery* models (802/803) have
  spotty vendor adoption; much battery data hides in proprietary registers.
  SunSpec model discovery is the exemplar; a config-driven generic Modbus
  register-map mode (like `csvtail` for registers) is what covers the
  long tail. Read function codes only (invariant 1).
- **CAN / DBC** (Archetype D, `python-can` + `cantools`): listen-only
  (invariant 1). **Decision made, not dodged** ●new: the core stays
  config-driven (bring-your-own `.dbc`, battfeed never guesses signal maps),
  and a **curated `contrib/` map collection** for common BMSes (Orion, REC,
  Batrium, …) grows by community contribution — adoption needs curated maps;
  the core stays unopinionated.
- **Minimal `--config file.toml`** lands here (register maps, `.dbc` paths,
  scalings exceed `--opt`), together with ●new **credential hygiene pulled
  forward from Phase 4**: env-var conventions and log/CLI redaction, because
  P3 sources already carry credentials-adjacent config and `--opt` secrets
  leak into shell history and process lists today.

*Done when: a SunSpec-conformant inverter and a replayed CAN bus each
produce valid BDF from config alone; no write/transmit path exists in
either transport (verified by test).*

### Phase 3½ — Lab cyclers (opportunistic) ●new

**In scope, scheduled by instrument access, not by calendar.** First target:
**Neware BTS server protocol** (widest installed base in research labs);
then Arbin/Biologic/Maccor live interfaces as access allows. Each is
Archetype I: a vendor TCP/server protocol feeding `StreamingSource` or plain
polling, multi-channel → `RoutingSink` (one channel = one series; one test
step schedule = runs). This is deliberately its own phase marker so it can
interleave with P2–P4 the moment a lab connection exists — it must never
again fall off the plan by omission.

### Phase 4 — Cloud + feed

- **`TokenStore` / secrets layer** (env → config → optional keyring; the
  redaction half already landed in P3), with shared OAuth-refresh machinery.
- **Ecoflow first, Tesla second** (both reuse P1's `RoutingSink`).
  ⚠ Eyes open on both: Ecoflow's IoT Open Platform needs **developer-program
  approval** and HMAC request signing, with device/region coverage varying —
  cheaper than Tesla, not "key + secret and go"; **verify access before the
  DoD depends on it**. Tesla Fleet API adds developer registration,
  virtual-key pairing, and per-call metered billing on vehicle data — verify
  current terms at build time.
- **Push sinks**: HTTP-ingest to the proprietary platform / BDA registry, plus Parquet.
  This is where the **BDF ↔ `telemetry_record` translation** is resolved —
  battfeed standardizes on BDF, the proprietary sink on
  `telemetry_record`; one side translates or the platform accepts BDF.
- **proprietary-collector sunset** ●new: P1 begins double-maintaining DJI across two
  repos with two schemas. By end of P4, the proprietary collectors become thin
  wrappers over battfeed sources or are retired — stated here so divergence
  is a decision, not an accident.
- **MQTT / Home Assistant bridge source** (Archetype F): nearly free once
  `StreamingSource` and the push sink exist, and strategically outsized — it
  inherits the HA ecosystem's device coverage (see archetype note).

*Done when: one cloud account fans out to N per-device BDF feeds with tokens
refreshed automatically; a live feed reaches the proprietary platform; its
collector sunset is underway.*

### Operations workstream (cross-cutting) ●new

"Foundational infrastructure" cannot be a foreground CLI ended by Ctrl-C.
Alongside P2–P3:

- **Unattended running**: documented service recipes (Windows Task
  Scheduler / systemd), restart-on-crash, machine-sleep behavior.
- **`battfeed run --config`**: a single-host supervisor collecting from
  **multiple sources concurrently** (a real site has a shunt + an inverter +
  an EV; today that is N processes and no supervisor). Single-host only —
  fleet management stays out of scope (the proprietary platform's job).
- **CI reality** ⚠: primary development is on Windows; CAN tooling is
  Linux-first (no socketcan on Windows) and BLE scanning differs per OS.
  CI grows a Linux leg; local P2/P3 development is replay-first by design.

### Other cross-cutting, throughout

- A published **source-contract test kit** run against mocks/replay fixtures
  — this and the cookbook are *deliverables with the same status as sources*,
  because battfeed's universality ceiling is set by third-party adoption,
  not by built-in count (see below).
- A per-archetype **source cookbook**.
- **BDF vocabulary coordination** (SOC / SOH / per-cell / temperature
  arrays) with the BDF spec and BattINFO — decoupled from phase critical
  paths by the P1 extension-namespace rule.
- Every new dependency behind an extra (`battfeed[dji]`, `[victron]`,
  `[sunspec]`, `[can]`, …).

---

## DJI reuse detail

The proprietary DJI collector (private; paths in the untracked coordination notes)
is complete and tested (26 tests, real Mavic Air 2 logs). Reuse, don't
rewrite:

- **`parser.py`** — the `dji-log` CLI wrapper: `.txt` vs `.DAT`
  classification, `DJI_API_KEY` handling for encrypted v13+ records, binary
  resolution, key redaction. Drop-in; the external binary maps onto
  `availability()` (`shutil.which("dji-log")`), like `android` needs `adb`.
- **The plausibility gate** in `normalize.py` — drops epoch-zero pre-GPS
  rows and corrupt-tail rows (pack voltage vs. per-cell sum > 0.5 V).
  Hard-won field knowledge; keep verbatim.

Adaptations: re-target `telemetry_record` → BDF columns; source-supplied
per-flight `test_time_second` with absolute start in `metadata()`;
`stream_key` → `series_id`, `run_uid` → `run_id`; flight context (GPS,
height, motor state) → sidecar, deliberately.

---

## How far does this plan actually get us?

An honest self-assessment against "the universal layer for bringing battery
data into the digital environment":

- **Universal *capability* — yes.** After P1 + P2 the contract expresses
  every known delivery shape: pull, push, batch, multi-object, bounded and
  unbounded. From then on, no source category requires architectural work.
- **Universal *coverage* — only through the ecosystem.** Nine archetypes with
  one exemplar each ≠ every device; the per-device protocol work (BLE BMS
  dialects, vendor register maps) does not amortize. The plugin seam, the
  contract test kit, the cookbook, and the HA/MQTT bridge are therefore the
  highest-leverage items in this plan — battfeed becomes universal when
  *other people* ship `battfeed.sources` entry points, and that succeeds or
  fails on developer experience.
- **The format is the interim, the feed is the point.** Rotation makes
  unbounded telemetry valid BDF, but files remain an interchange for a
  format born in the lab. For battwin and the proprietary platform the native end state
  is the P4 push feed; measure the plan against that, not against file
  counts.
- **Success metric**: third-party sources shipped by people who are not us,
  and battwin/the proprietary platform consuming battfeed as their *only* ingestion
  path. Built-in source count is a vanity metric.
