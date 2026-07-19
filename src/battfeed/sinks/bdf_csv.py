"""Write collected samples as a BDF (Battery Data Format) CSV file.

BDF files use snake_case machine-readable headers of the form
``{quantity}_{unit}``. Every conforming file carries the required trio
``test_time_second``, ``voltage_volt`` and ``current_ampere``.

Sign convention (per the Battery Data Format specification, and used
throughout battfeed): **positive current charges the test object (current
flows into it); negative current discharges it.** Power follows the same
sign as current.
"""

from __future__ import annotations

import csv
import datetime
import json
import logging
import os
import time
from datetime import timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, TextIO

from ..protocols import RESERVED_KEYS, SampleValue

__all__ = ["REQUIRED_COLUMNS", "BdfCsvSink", "dataset_filename", "validate_file"]

logger = logging.getLogger(__name__)

#: The trio every BDF file must contain, in the order they lead the header.
REQUIRED_COLUMNS: tuple[str, ...] = ("test_time_second", "voltage_volt", "current_ampere")

#: Routing keys stripped from every row and column set (see ``protocols.RESERVED_KEYS``).
_RESERVED: frozenset[str] = frozenset(RESERVED_KEYS)

#: Wall-clock seconds between mid-collection sidecar rewrites. Cheap enough to
#: keep on-disk metadata current for an unbounded stream without rewriting the
#: sidecar on every flush.
_SIDECAR_REWRITE_INTERVAL_S = 60.0

_BDF_SUFFIX = ".bdf.csv"


def dataset_filename(institution: str, cell_name: str, date: datetime.date, seq: int) -> str:
    """Build a BDF dataset file name: ``InstitutionCode__CellName__YYYYMMDD_XXX.bdf.csv``.

    Example::

        >>> dataset_filename("SINTEF", "CR2032-01", datetime.date(2026, 7, 7), 3)
        'SINTEF__CR2032-01__20260707_003.bdf.csv'
    """
    for label, value in (("institution", institution), ("cell_name", cell_name)):
        if not value:
            raise ValueError(f"{label} must be non-empty")
        if "__" in value:
            raise ValueError(
                f"{label} must not contain '__' (reserved as the BDF filename separator): {value!r}"
            )
    if not 0 <= seq <= 999:
        raise ValueError(f"seq must be between 0 and 999, got {seq}")
    return f"{institution}__{cell_name}__{date:%Y%m%d}_{seq:03d}{_BDF_SUFFIX}"


def _ordered_columns(columns: Iterable[str]) -> list[str]:
    """Required trio first (fixed order), then everything else sorted."""
    extras = sorted(set(columns) - set(REQUIRED_COLUMNS))
    return [*REQUIRED_COLUMNS, *extras]


class BdfCsvSink:
    """Stream samples into a ``.bdf.csv`` file with a JSON metadata sidecar.

    The header always leads with the required trio ``test_time_second``,
    ``voltage_volt``, ``current_ampere`` (in that order) followed by any
    extra columns sorted alphabetically. If ``columns`` is not given, the
    column set is inferred from the first batch written; later samples with
    unknown extra keys are dropped from the file (with a debug log), and
    samples missing a column leave that cell empty.

    The routing keys in :data:`battfeed.RESERVED_KEYS` (``series_id`` /
    ``run_id``) are stripped from every row and from any explicit column set
    before header inference and writing, so a routing-aware source wired
    directly to this plain sink can never leak them into CSV columns.

    A sidecar ``<name>.meta.json`` (the ``.bdf.csv`` suffix replaced) is
    written next to the data file. It contains the ``metadata`` mapping plus
    the started/finished timestamps, the battfeed version, the column list,
    the row count, and a ``finalized`` flag. To survive a crash mid-collection
    the sidecar is written **early** -- as soon as the data file is first
    opened (``finalized: false``) -- rewritten periodically as rows accumulate,
    and rewritten a final time on :meth:`close` with ``finalized: true`` and
    the final row count. A sidecar with ``finalized: false`` therefore marks a
    data file whose collection did not finish cleanly.

    Args:
        path: Output file path, conventionally named via
            :func:`dataset_filename`.
        columns: Optional explicit column set (order-insensitive; the header
            ordering rule above is applied regardless).
        metadata: Optional JSON-serialisable mapping recorded in the sidecar
            (operator, cell id, instrument settings, ...).
        clock: Monotonic clock used only to pace mid-collection sidecar
            rewrites; injectable so tests can drive it without wall time.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        columns: Sequence[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._path = Path(path)
        self._explicit_columns = list(columns) if columns is not None else None
        self._metadata = dict(metadata or {})
        self._clock = clock
        self._started_at = self._utcnow()
        self._columns: list[str] | None = None
        self._file: TextIO | None = None
        self._writer: csv.DictWriter | None = None
        self._rows_written = 0
        self._last_sidecar_at = 0.0
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def _utcnow() -> str:
        return datetime.datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _open(self, first_batch: Sequence[Mapping[str, SampleValue]]) -> None:
        inferred = self._explicit_columns
        if inferred is None:
            seen: set[str] = set()
            for row in first_batch:
                seen.update(row)
            inferred = list(seen)
        # Reserved routing keys are contract metadata, never columns -- strip
        # them from an explicit column set too (rows are already stripped).
        self._columns = _ordered_columns(c for c in inferred if c not in _RESERVED)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(
            self._file, fieldnames=self._columns, restval="", extrasaction="ignore"
        )
        self._writer.writeheader()
        self._file.flush()
        logger.info("Writing BDF CSV %s with columns %s", self._path, self._columns)
        # Early sidecar: a crash before close() must still leave metadata on
        # disk next to the data file (marked unfinalised).
        self._write_sidecar(finalized=False)
        self._last_sidecar_at = self._clock()

    def write(self, rows: Iterable[Mapping[str, SampleValue]]) -> None:
        """Append a batch of samples to the file (opens it on first use).

        Reserved routing keys (``series_id`` / ``run_id``) are stripped from
        every row before header inference and writing.
        """
        if self._closed:
            raise ValueError(f"BdfCsvSink for {self._path} is closed")
        batch = [{key: value for key, value in row.items() if key not in _RESERVED} for row in rows]
        if not batch:
            return
        if self._writer is None:
            self._open(batch)
        assert self._writer is not None and self._columns is not None
        known = set(self._columns)
        for row in batch:
            dropped = row.keys() - known
            if dropped:
                logger.debug("Dropping columns not in header for %s: %s", self._path, dropped)
            self._writer.writerow(row)
        self._rows_written += len(batch)
        # Long-running field collection must survive a crash: flush every batch
        # (cheap at polling rates) so data on disk stays current.
        assert self._file is not None
        self._file.flush()
        # Refresh the sidecar periodically (not every flush) so an unbounded
        # stream keeps a current, valid-but-unfinalised sidecar on disk.
        now = self._clock()
        if now - self._last_sidecar_at >= _SIDECAR_REWRITE_INTERVAL_S:
            self._write_sidecar(finalized=False)
            self._last_sidecar_at = now

    def close(self) -> None:
        """Close the CSV file and finalise the ``.meta.json`` sidecar. Idempotent."""
        if self._closed:
            return
        if self._writer is None:
            # Nothing was written: still emit a valid, empty BDF file.
            self._open([])
        assert self._file is not None
        self._file.close()
        self._file = None
        self._closed = True
        self._write_sidecar(finalized=True)

    def _sidecar_path(self) -> Path:
        name = self._path.name
        if name.endswith(_BDF_SUFFIX):
            stem = name[: -len(_BDF_SUFFIX)]
        else:
            stem = self._path.stem
        return self._path.with_name(stem + ".meta.json")

    def _write_sidecar(self, *, finalized: bool) -> None:
        from battfeed import __version__  # local import to avoid a cycle at module load

        sidecar = {
            "file": self._path.name,
            "metadata": self._metadata,
            "started_at": self._started_at,
            "finished_at": self._utcnow() if finalized else None,
            "battfeed_version": __version__,
            "columns": self._columns or [],
            "rows": self._rows_written,
            "finalized": finalized,
        }
        path = self._sidecar_path()
        # Write-then-replace so a crash during the write cannot leave a reader
        # staring at a half-written (invalid JSON) sidecar; os.replace is atomic.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(sidecar, indent=2, default=str) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        logger.info("Wrote sidecar %s (finalized=%s)", path, finalized)


def validate_file(path: str | Path) -> dict[str, Any]:
    """Validate an emitted file with the ``batterydf`` package (optional extra).

    Returns the validation report dict from ``bdf.validate`` (it contains at
    least an ``"ok"`` boolean). battfeed itself never parses or normalises
    vendor data; this simply hands the finished file to the reference
    implementation of the format.

    Raises:
        ImportError: if ``batterydf`` is not installed -- install it with
            ``pip install "battfeed[bdf]"``.
        RuntimeError: if ``batterydf`` fails while validating the file.
    """
    try:
        import bdf  # the import name of the 'batterydf' distribution
    except ImportError as exc:
        raise ImportError(
            "validate_file needs the optional 'batterydf' package, which is "
            'not installed. Install it with: pip install "battfeed[bdf]".'
        ) from exc
    try:
        report = bdf.validate(str(path))
    except Exception as exc:
        raise RuntimeError(f"batterydf failed to validate {path}: {exc}") from exc
    if not report.get("ok", False):
        logger.warning("batterydf reports %s as not OK: %s", path, report)
    return report
