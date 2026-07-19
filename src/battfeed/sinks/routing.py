"""Demultiplex one sample stream into one BDF file per (series, run), with rotation.

BDF's invariant is **one test object, one monotonic timebase, per file** -- but
the world violates it in three directions: one connection can yield many
objects (an account with N cars, a drone with N packs), one object can yield
many runs (a pack flies many flights, each restarting its clock), and many
sources never end at all (a shunt streams forever). :class:`RoutingSink`
resolves all three at the sink layer:

* Samples carrying the reserved routing keys (``series_id`` / ``run_id``, see
  :data:`battfeed.RESERVED_KEYS`) are demultiplexed into one child
  :class:`~battfeed.BdfCsvSink` per ``(series_id, run_id)``. Samples without
  ``series_id`` flow to a single default stream, so routing-free sources work
  unchanged.
* A new ``run_id`` for a known series closes the previous file and opens the
  next one -- runs never merge into a non-monotonic timebase.
* **Rotation** (``rotate_after_s`` / ``rotate_after_rows``, whichever trips
  first) does the same *without* a ``run_id`` change: segments *are* runs for
  endless streams, turning unbounded telemetry into a sequence of bounded,
  valid BDF files (each finalized with its sidecar as soon as it rotates, so a
  crash loses at most the open segments -- and even those keep the early,
  unfinalised sidecar the child sink writes at open time).

Filenames follow the BDF convention via
:func:`~battfeed.sinks.bdf_csv.dataset_filename`; the ``_XXX`` sequence slot
advances per run/segment for the same series on the same day. Raw
``series_id`` values are device serials, not filenames -- they may contain
``__`` (reserved as the BDF filename separator), path separators, characters
Windows forbids, or be empty -- so they pass through
:func:`sanitize_cell_name`, with deterministic hash-suffix disambiguation when
two distinct ids sanitize to the same name.

**Timebase ownership (invariant I5).** This sink routes and never restamps:
``test_time_second`` is written exactly as the source supplied it. A source
that emits routing keys must therefore supply its own ``test_time_second``,
zero-based per (series, run) -- the harvester's shared elapsed-collection
stamp is wrong for an object that appears mid-run (see
:mod:`battfeed.protocols`).
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ..protocols import RESERVED_KEYS, SampleValue, Sink
from .bdf_csv import BdfCsvSink, dataset_filename

__all__ = ["RoutingSink", "sanitize_cell_name"]

logger = logging.getLogger(__name__)

#: Routing keys stripped from every row before delegation (defense in depth --
#: the child sink strips them again; see ``protocols.RESERVED_KEYS``).
_RESERVED: frozenset[str] = frozenset(RESERVED_KEYS)

#: Cell name used for samples that carry no ``series_id``. Reserved at
#: construction so a literal series id ``"default"`` can never collide with it.
_DEFAULT_CELL_NAME = "default"

#: Cap on sanitized cell names, keeping full dataset filenames comfortably
#: inside Windows path limits.
_MAX_CELL_NAME_LEN = 60

#: Characters illegal in Windows filenames (plus the path separators).
_ILLEGAL_CHARS = frozenset('<>:"/\\|?*')


def sanitize_cell_name(raw: str) -> str:
    """Turn a raw series id (e.g. a device serial) into a safe BDF cell name.

    Raw ids are whatever the device reports: they may contain ``__`` (reserved
    as the BDF filename separator), path separators, characters illegal on
    Windows, control characters, or be empty. The result is deterministic,
    non-empty, at most 60 characters, and never contains ``__`` -- nor starts
    or ends with ``_`` (which would recreate ``__`` next to the filename
    separators).

    Distinct raw ids can sanitize to the same name (``"pack/1"`` and
    ``"pack?1"`` both become ``"pack-1"``, and names differing only by case
    collide too -- Windows filesystems are case-insensitive);
    :class:`RoutingSink` disambiguates such collisions with a stable hash
    suffix derived from the raw id.
    """
    cleaned = "".join("-" if ch in _ILLEGAL_CHARS or ord(ch) < 32 else ch for ch in raw)
    cleaned = re.sub(r"\s+", "-", cleaned)
    cleaned = re.sub(r"_{2,}", "-", cleaned)  # "__" is the BDF filename separator
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    cleaned = cleaned[:_MAX_CELL_NAME_LEN]
    cleaned = cleaned.strip("-._ ")
    return cleaned or "unknown"


@dataclass
class _SeriesState:
    """Book-keeping for one demultiplexed series (or the default stream)."""

    series_id: str | None
    cell_name: str
    series_metadata: dict[str, Any]
    run_id: str | None = None
    segment: int = 0
    sink: Sink | None = None
    opened_at: float = 0.0
    rows_in_segment: int = 0
    files: list[Path] = field(default_factory=list)


class RoutingSink:
    """Route one sample stream into one BDF file per (series, run), rotating.

    Implements the :class:`battfeed.Sink` protocol, so it drops in anywhere a
    plain :class:`battfeed.BdfCsvSink` does -- the harvester never knows the
    difference. Child files open lazily as series/runs appear, so a series that
    comes online mid-stream gets its own file and sidecar from its first
    sample; closing a segment (run change, rotation, or :meth:`close`)
    finalizes that segment's sidecar immediately.

    Per-segment sidecar metadata is the shared ``metadata`` mapping, overlaid
    with the ``series_info`` metadata for the series, overlaid with
    ``{"series_id": ..., "run_id": ..., "segment": n}`` (``segment`` counts
    from 1 per series).

    Args:
        directory: Directory the ``.bdf.csv`` files (and their sidecars) are
            written into; created on demand.
        institution: Institution code for :func:`dataset_filename`.
        series_info: Optional ``callable(series_id) -> (cell_name,
            metadata_dict)`` hook so lazily-discovered objects get proper
            filenames and their own sidecar content. Called once per new
            series; without it the sanitized ``series_id`` names the cell.
        metadata: Shared base metadata recorded in every segment's sidecar.
        rotate_after_s: Close and re-open a series' file once it has been open
            this many seconds (measured with ``clock``).
        rotate_after_rows: Close and re-open a series' file once it holds this
            many rows. With both limits set, whichever trips first rotates.
        sink_factory: ``callable(path, *, metadata) -> Sink`` building each
            child sink; injectable so tests can capture routed rows in memory.
            (Path allocation still reserves each claimed path on disk as an
            empty file -- see :meth:`_next_path` -- regardless of the factory.)
        clock: Monotonic clock driving time-based rotation; injectable for
            tests (see the Harvester's clock/sleep injection).
        today: Date provider for filenames; injectable so tests get
            deterministic names.

    Unlike ``BdfCsvSink``, closing without ever writing produces no files:
    there is nothing to route, so nothing is (even emptily) recorded.
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        institution: str = "LOCAL",
        series_info: Callable[[str], tuple[str, Mapping[str, Any]]] | None = None,
        metadata: Mapping[str, Any] | None = None,
        rotate_after_s: float | None = None,
        rotate_after_rows: int | None = None,
        sink_factory: Callable[..., Sink] = BdfCsvSink,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], datetime.date] = datetime.date.today,
    ) -> None:
        if not institution:
            raise ValueError("institution must be non-empty")
        if "__" in institution:
            raise ValueError(
                "institution must not contain '__' (reserved as the BDF filename "
                f"separator): {institution!r}"
            )
        if rotate_after_s is not None and rotate_after_s <= 0:
            raise ValueError(f"rotate_after_s must be positive, got {rotate_after_s}")
        if rotate_after_rows is not None and rotate_after_rows < 1:
            raise ValueError(f"rotate_after_rows must be >= 1, got {rotate_after_rows}")
        self._directory = Path(directory)
        self._institution = institution
        self._series_info = series_info
        self._metadata = dict(metadata or {})
        self._rotate_after_s = rotate_after_s
        self._rotate_after_rows = rotate_after_rows
        self._sink_factory = sink_factory
        self._clock = clock
        self._today = today
        self._series: dict[str | None, _SeriesState] = {}
        # casefolded cell_name -> raw series id that owns it ("" owns the
        # default stream). Keyed case-insensitively because the dominant
        # filesystems are: "CellA" and "cella" would be one file on NTFS.
        self._claimed: dict[str, str] = {_DEFAULT_CELL_NAME: ""}
        self._seq: dict[tuple[str, str], int] = {}
        self._closed = False

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def files_by_series(self) -> dict[str | None, list[Path]]:
        """Paths written so far, keyed by ``series_id`` (``None`` = default stream).

        Segments appear in the order they were opened; the last entry of a list
        may still be open. Returns a copy.
        """
        return {series: list(state.files) for series, state in self._series.items()}

    def write(self, rows: Iterable[Mapping[str, SampleValue]]) -> None:
        """Route a batch of samples to their per-(series, run) child sinks.

        Reserved routing keys are stripped from every row before delegation;
        everything else -- including any source-supplied ``test_time_second``
        -- is passed through untouched (invariant I5: the source owns the
        per-(series, run) timebase).
        """
        if self._closed:
            raise ValueError(f"RoutingSink for {self._directory} is closed")
        grouped: dict[str | None, list[tuple[str | None, dict[str, SampleValue]]]] = {}
        for row in rows:
            series_value = row.get("series_id")
            run_value = row.get("run_id")
            series = str(series_value) if series_value is not None else None
            run = str(run_value) if run_value is not None else None
            stripped = {key: value for key, value in row.items() if key not in _RESERVED}
            grouped.setdefault(series, []).append((run, stripped))
        for series, items in grouped.items():
            self._write_series(self._state_for(series), items)

    def close(self) -> None:
        """Close every open child sink (finalising its sidecar). Idempotent."""
        if self._closed:
            return
        self._closed = True
        first_error: Exception | None = None
        for state in self._series.values():
            if state.sink is None:
                continue
            try:
                self._close_segment(state)
            except Exception as exc:  # noqa: BLE001 -- keep closing the other children
                logger.exception("Failed to close child sink for series %r", state.series_id)
                if first_error is None:
                    first_error = exc
                state.sink = None
        if first_error is not None:
            raise first_error

    # -- internals --------------------------------------------------------

    def _state_for(self, series: str | None) -> _SeriesState:
        state = self._series.get(series)
        if state is not None:
            return state
        if series is None:
            cell_name = _DEFAULT_CELL_NAME
            series_metadata: dict[str, Any] = {}
        else:
            if self._series_info is not None:
                info_name, info_metadata = self._series_info(series)
                base = sanitize_cell_name(info_name)
                series_metadata = dict(info_metadata)
            else:
                base = sanitize_cell_name(series)
                series_metadata = {}
            cell_name = self._allocate_cell_name(base, series)
        state = _SeriesState(series_id=series, cell_name=cell_name, series_metadata=series_metadata)
        self._series[series] = state
        logger.info("New series %r routed to cell name %r", series, cell_name)
        return state

    def _allocate_cell_name(self, base: str, series: str) -> str:
        """Claim a unique cell name for ``series``, hash-suffixing collisions.

        Claims are matched **case-insensitively** (casefolded), because the
        filesystems this targets are: two names differing only by case would
        entangle on NTFS/APFS. The returned name keeps the original casing.

        Plain (unsuffixed) names are **first-come-first-served**: the first
        series to sanitize to ``base`` keeps the plain name, and every later
        distinct series gets a stable suffix derived from its raw id. Filename
        assignment is therefore **arrival-order dependent across runs** -- the
        same series can be plain in one collection and suffixed in the next if
        its rival arrived first. Do not join datasets on filenames: the
        sidecar's recorded raw ``series_id`` is the stable join key.
        """
        if base.casefold() not in self._claimed:
            self._claimed[base.casefold()] = series
            return base
        digest = hashlib.sha256(series.encode("utf-8")).hexdigest()
        for length in (8, 16, 32, 64):
            candidate = f"{base}-{digest[:length]}"
            if candidate.casefold() not in self._claimed:
                self._claimed[candidate.casefold()] = series
                logger.info(
                    "Series %r collides with %r on cell name %r; using %r",
                    series,
                    self._claimed[base.casefold()],
                    base,
                    candidate,
                )
                return candidate
        raise RuntimeError(  # pragma: no cover -- would need a sha256 collision
            f"Could not allocate a unique cell name for series {series!r}"
        )

    def _write_series(
        self,
        state: _SeriesState,
        items: list[tuple[str | None, dict[str, SampleValue]]],
    ) -> None:
        index = 0
        while index < len(items):
            run_id = items[index][0]
            if state.sink is not None and (run_id != state.run_id or self._rotation_due(state)):
                self._close_segment(state)
            if state.sink is None:
                self._open_segment(state, run_id)
            end = index
            while end < len(items) and items[end][0] == run_id:
                end += 1
            chunk = [row for _, row in items[index:end]]
            if self._rotate_after_rows is not None:
                chunk = chunk[: self._rotate_after_rows - state.rows_in_segment]
            assert state.sink is not None
            state.sink.write(chunk)
            state.rows_in_segment += len(chunk)
            index += len(chunk)
            if (
                self._rotate_after_rows is not None
                and state.rows_in_segment >= self._rotate_after_rows
            ):
                # Rotate eagerly at the row limit so a full segment is
                # finalized (sidecar and all) the moment it fills.
                self._close_segment(state)

    def _rotation_due(self, state: _SeriesState) -> bool:
        return (
            self._rotate_after_s is not None
            and self._clock() - state.opened_at >= self._rotate_after_s
        )

    def _open_segment(self, state: _SeriesState, run_id: str | None) -> None:
        state.segment += 1
        state.run_id = run_id
        path = self._next_path(state.cell_name)
        segment_metadata: dict[str, Any] = {
            **self._metadata,
            **state.series_metadata,
            "series_id": state.series_id,
            "run_id": run_id,
            "segment": state.segment,
        }
        state.sink = self._sink_factory(path, metadata=segment_metadata)
        state.opened_at = self._clock()
        state.rows_in_segment = 0
        state.files.append(path)
        logger.info(
            "Opened segment %d for series %r (run %r): %s",
            state.segment,
            state.series_id,
            run_id,
            path,
        )

    def _next_path(self, cell_name: str) -> Path:
        """Next free dataset path: the ``_XXX`` slot advances per (cell, day).

        The claimed path is **reserved atomically** at allocation time by
        creating it with ``open(..., "x")`` (``O_CREAT | O_EXCL``): a bare
        ``exists()`` check would leave a check-then-claim window in which two
        concurrent collections into the same directory could pick the same
        sequence number and silently clobber each other's file. On
        ``FileExistsError`` the sequence number advances and the claim is
        retried (``dataset_filename`` raises loudly past 999).

        The reservation is just an empty file; the child ``BdfCsvSink`` later
        reopens it with mode ``"w"``, truncating it before writing the header,
        so nothing downstream changes. Segments open lazily, only when a row
        is about to be written, so a reservation is never left row-less by
        normal operation -- only a crash inside the reserve-to-write window
        can leave an empty ``.bdf.csv``, and that file still (correctly)
        marks its sequence number as taken.
        """
        date = self._today()
        key = (cell_name.casefold(), date.isoformat())
        seq = self._seq.get(key, 1)
        self._directory.mkdir(parents=True, exist_ok=True)
        while True:
            path = self._directory / dataset_filename(self._institution, cell_name, date, seq)
            try:
                with open(path, "x", encoding="utf-8"):
                    pass
            except FileExistsError:
                seq += 1
                continue
            break
        self._seq[key] = seq + 1
        return path

    def _close_segment(self, state: _SeriesState) -> None:
        assert state.sink is not None
        sink = state.sink
        state.sink = None
        sink.close()
        logger.info(
            "Closed segment %d of series %r (%d row(s))",
            state.segment,
            state.series_id,
            state.rows_in_segment,
        )
