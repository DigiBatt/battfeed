"""DJI flight-log import source: one flight file per poll, routed per pack.

``DjiFlightLogSource`` is a folder-watching :class:`~battfeed.DataSource` (one
seam, invariant I3) built on the WP1.3 batch-import machinery. Each
:meth:`poll` ingests the *next* un-ingested record file: it parses the raw log
with ``dji-log`` (:mod:`.parser`), runs the rows through the plausibility gate
(:mod:`.gate`), and returns all rows of that one flight as a single batch,
carrying ``series_id`` (per pack), ``run_id`` (per flight) and a
zero-based-per-flight ``test_time_second`` (invariant I5).

The commit point is deferred: :meth:`poll` never touches the ledger, and the
file is recorded as ingested only in :meth:`commit_batch`, which
:func:`battfeed.run_import` calls **after** the sink write succeeds
(at-least-once; see :mod:`battfeed.importer`). ``.DAT`` aircraft logs and other
recognized-but-unparseable files are quarantined with a reason and never
retried; a transient parse failure (missing API key, missing binary) is
*raised* so the driver's :class:`~battfeed.ErrorPolicy` owns retry (invariant
I4).

The external ``dji-log`` binary is required at run time (like the Android
source needs ``adb``); it is discovered via ``dji_log_bin`` /
``DJI_LOG_BIN`` / ``PATH`` and :meth:`availability` reports its absence.
Decrypting format-v13+ records is **not offline** -- it makes a network call to
DJI's keychain API (see :mod:`.parser`).
"""

from __future__ import annotations

import csv
import logging
import tempfile
from pathlib import Path
from typing import Any, Mapping

from ...ingest_state import ImportLedger
from .gate import FlightData, cell_column, normalize_flight
from .parser import ParseError, UnsupportedFormat, find_binary, parse_flight

__all__ = ["DjiFlightLogSource"]

logger = logging.getLogger(__name__)

#: File suffixes considered by a directory scan, matched case-insensitively so a
#: POSIX filesystem still finds ``.TXT`` / ``.DAT`` as readily as ``.txt``.
_RECORD_SUFFIXES = (".txt", ".dat")

#: Malformed-output exceptions from a pathological dji-log CSV that must be
#: quarantined (a permanent property of THIS file), never retried to failure.
_MALFORMED_OUTPUT = (csv.Error, ValueError, UnicodeError)

#: Highest per-cell extension column battfeed declares up front (a DJI pack is
#: at most a 14S; the actual columns emitted depend on each flight's cell count).
_MAX_DECLARED_CELLS = 14


class DjiFlightLogSource:
    """Import DJI Fly app flight records as per-(pack, flight) BDF feeds.

    Point it at a single record file **or** a directory of them. Each
    :meth:`poll` ingests one un-ingested file; run it through
    :func:`battfeed.run_import` (the ``battfeed import`` verb) to drain a folder
    into one ``.bdf.csv`` (plus sidecar) per ``(series_id, run_id)`` via a
    :class:`~battfeed.RoutingSink`.

    Emitted BDF columns (see :mod:`.gate` for the full unit/sign notes):
    ``test_time_second`` (zero-based per flight), ``voltage_volt``,
    ``current_ampere`` (**negated** -- DJI reports positive = draw from the
    pack, BDF positive = charging), ``power_watt``,
    ``surface_temperature_celsius``, and per-cell ``cell_N_voltage_volt``
    **extension** columns. Routing keys: ``series_id`` =
    ``"<aircraftSerial>:<batterySerial>"`` (a missing serial falls back to a
    content-hash-derived id so distinct anonymous packs never merge), ``run_id``
    = ``"flight-<sha256[:12] of the raw file>"``. Flight context (GPS, height,
    motor state, capacity) is carried in the per-flight summary, never as BDF
    columns and never silently dropped (invariants I2/I5).

    Args:
        path: A single record file, or a directory scanned for ``.txt`` /
            ``.dat`` records (suffix matched case-insensitively).
        ledger_path: Where the dedupe/quarantine ledger lives (default:
            ``.battfeed-dji-ledger.json`` beside the data). Deleting output
            files never resets it -- use ``battfeed import --reset-ledger``.
        dji_log_bin: Explicit ``dji-log`` path (default: ``DJI_LOG_BIN`` env or
            ``PATH``).
        api_key: DJI keychain API key for encrypted (format-v13+) records
            (default: the ``DJI_API_KEY`` env var). Redacted from all logging.
        include_gps: Carry the last GPS fix in the per-flight summary (default:
            off -- flight logs are location data).
    """

    def __init__(
        self,
        path: str | Path,
        ledger_path: str | Path | None = None,
        dji_log_bin: str | None = None,
        api_key: str | None = None,
        include_gps: bool = False,
    ) -> None:
        self.name = "dji"
        self._path = Path(path)
        if ledger_path is not None:
            ledger_file = Path(ledger_path)
        else:
            base = self._path if self._path.is_dir() else self._path.parent
            ledger_file = base / ".battfeed-dji-ledger.json"
        self._ledger = ImportLedger(ledger_file)
        self._dji_log_bin = dji_log_bin
        self._api_key = api_key
        self._include_gps = bool(include_gps)
        # (path, content_hash) awaiting commit_batch(); the commit point.
        self._pending: tuple[Path, str] | None = None
        # Content hashes already ingested or quarantined THIS process: a poll
        # checks this first and skips silently, so a quarantined file does not
        # log an info "skip" line on every poll of a watched folder (the ledger
        # still holds the durable truth; this is only a per-process log damper).
        self._known_skip: set[str] = set()
        # Per-flight summaries produced so far, surfaced through metadata().
        self._flights: list[dict[str, Any]] = []
        self._extension_columns: list[str] = []

    @classmethod
    def availability(cls) -> str | None:
        """Return ``None`` if this source can run here, else why it cannot.

        Used by the ``battfeed sources`` listing; checks for the external
        ``dji-log`` binary with :func:`.parser.find_binary` (honouring
        ``DJI_LOG_BIN``) and never executes it or touches the network.
        """
        if find_binary() is None:
            return (
                "requires the external 'dji-log' binary "
                "(https://github.com/lvauvillier/dji-log-parser); put it on PATH "
                "or set DJI_LOG_BIN"
            )
        return None

    def metadata(self) -> Mapping[str, Any]:
        """Describe the source: kind, binary, extension columns, caveats.

        ``extension_columns`` lists the per-cell columns so the sink sidecars
        can declare them; ``flights`` accumulates the per-flight summaries
        (identity, drop counts, time/SOC window, flight context) produced so
        far, so nothing is silently dropped (invariant I2).
        """
        declared = self._extension_columns or [
            cell_column(i) for i in range(1, _MAX_DECLARED_CELLS + 1)
        ]
        return {
            "source": self.name,
            "kind": "dji-flight-log",
            "path": str(self._path),
            "dji_log_bin": find_binary(self._dji_log_bin),
            "include_gps": self._include_gps,
            "extension_columns": list(declared),
            "notes": (
                "Ported from a proprietary implementation. current_ampere and "
                "power_watt are NEGATED (dji-log reports positive = draw from the "
                "pack; BDF positive = charging). Decrypting format-v13+ records is "
                "NOT offline -- dji-log makes a network call to DJI's keychain API "
                "at parse time. Per-cell cell_N_voltage_volt columns are a "
                "non-canonical BDF extension (see extension_columns). Flight "
                "context (GPS/height/motor/capacity) is summarized per flight, "
                "never emitted as BDF columns."
            ),
            "flights": list(self._flights),
        }

    def poll(self) -> list[dict[str, Any]]:
        """Ingest the next un-ingested record file; return its flight as one batch.

        Scans in sorted order, hashing each candidate exactly once and reusing
        the digest for every ledger query. Already-ingested and quarantined
        files are skipped; ``.DAT`` / unparseable / all-implausible files are
        quarantined with a reason (and the scan continues to the next file so a
        bad file never costs a poll). A parseable flight's rows are returned and
        the file is remembered as *pending* -- recorded only in
        :meth:`commit_batch`. A transient :class:`~.parser.ParseError` is
        raised for the driver to retry.
        """
        for path in self._candidates():
            digest = self._ledger.hash_of(path)
            if digest is None:
                continue  # zero-byte: never content-keyed; re-judged next poll
            if self._is_skippable(path, digest):
                continue
            flight = self._ingest(path, digest)
            if flight is None:
                continue  # quarantined inside _ingest; try the next file
            if not flight.rows:
                self._quarantine(
                    path,
                    digest,
                    "parsed but produced no plausible rows (all rows failed the "
                    "timestamp/plausibility gate)",
                )
                continue
            self._pending = (path, digest)
            self._extension_columns = flight.extension_columns
            self._flights.append(flight.meta)
            logger.info(
                "Ingested %s: %d row(s), series %r run %r",
                path.name,
                len(flight.rows),
                flight.meta.get("series_id"),
                flight.meta.get("run_id"),
            )
            return flight.rows
        return []

    def drained(self) -> bool:
        """True when no un-ingested, non-quarantined record file remains."""
        for path in self._candidates():
            digest = self._ledger.hash_of(path)
            if digest is None:
                continue
            if self._is_skippable(path, digest):
                continue
            return False
        return True

    def commit_batch(self) -> None:
        """Record the just-written file as ingested (the commit point).

        Called by :func:`battfeed.run_import` only after ``sink.write``
        succeeded, so a crash before this leaves the file un-recorded and it
        re-imports next run (at-least-once; see :mod:`battfeed.ingest_state`).
        """
        if self._pending is not None:
            path, digest = self._pending
            self._ledger.record(path, content_hash=digest)
            self._known_skip.add(digest)
            self._pending = None

    def reset_ledger(self) -> None:
        """Clear the dedupe/quarantine ledger (``battfeed import --reset-ledger``)."""
        self._ledger.reset()
        self._known_skip.clear()
        self._pending = None

    def close(self) -> None:
        """Release resources. Idempotent; the source holds no open handles."""

    # -- internals -----------------------------------------------------------

    def _is_skippable(self, path: Path, digest: str) -> bool:
        """True if this content is already ingested or quarantined.

        The per-process ``_known_skip`` cache is consulted first so a settled
        file (ingested or quarantined) is skipped WITHOUT re-querying the ledger
        -- which logs an info "skip" line every time -- on each poll of a watched
        folder. The ledger remains the durable source of truth.
        """
        if digest in self._known_skip:
            return True
        if self._ledger.seen(path, content_hash=digest):
            self._known_skip.add(digest)
            return True
        if self._ledger.is_quarantined(path, content_hash=digest):
            self._known_skip.add(digest)
            return True
        return False

    def _quarantine(self, path: Path, digest: str, reason: str) -> None:
        """Quarantine ``path`` in the ledger and cache it so later polls skip it."""
        self._ledger.quarantine(path, reason, content_hash=digest)
        self._known_skip.add(digest)

    def _candidates(self) -> list[Path]:
        """Record files to consider, sorted and deduped.

        Matches by suffix (``.txt`` / ``.dat``) lower-cased, so a POSIX
        filesystem finds ``.TXT`` / ``.DAT`` too (case-literal globs would not).
        A source pointed at a single file returns just that file regardless of
        suffix (an explicit operator choice; dji-log is the final judge).
        """
        if not self._path.exists():
            return []
        if self._path.is_file():
            return [self._path]
        found: dict[str, Path] = {}
        for candidate in self._path.iterdir():
            if candidate.is_file() and candidate.suffix.lower() in _RECORD_SUFFIXES:
                found[str(candidate)] = candidate
        return [found[key] for key in sorted(found)]

    def _ingest(self, path: Path, digest: str) -> FlightData | None:
        """Parse + normalize one file; quarantine bad ones, raise transient.

        Returns the :class:`~.gate.FlightData` on success, or ``None`` when the
        file was quarantined (unsupported ``.DAT``/corrupt record, or a
        pathological dji-log CSV that trips :data:`_MALFORMED_OUTPUT`). Raises
        :class:`~.parser.ParseError` for retryable failures only.

        ``normalize_flight`` runs INSIDE this try so a poison-pill output (e.g.
        an oversized CSV field raising :class:`csv.Error`) is quarantined as a
        permanent property of THIS file -- not left to escape ``poll()`` as a
        generic error, which the driver would retry to ``SourceFailure`` and
        starve every later file in the folder.
        """
        with tempfile.TemporaryDirectory(prefix="battfeed-dji-") as tmp:
            out_csv = Path(tmp) / (path.stem + ".csv")
            try:
                parse_flight(
                    path,
                    out_csv,
                    binary=self._dji_log_bin,
                    api_key=self._api_key,
                )
                return normalize_flight(
                    out_csv,
                    digest,
                    include_gps=self._include_gps,
                    source_file=path.name,
                )
            except UnsupportedFormat as exc:
                self._quarantine(path, digest, str(exc))
                return None
            except ParseError:
                raise  # transient: let run_import's ErrorPolicy retry (invariant I4)
            except _MALFORMED_OUTPUT as exc:
                self._quarantine(path, digest, f"malformed dji-log output: {exc}")
                return None
