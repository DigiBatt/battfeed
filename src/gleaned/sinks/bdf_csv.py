"""Write collected samples as a BDF (Battery Data Format) CSV file.

BDF files use snake_case machine-readable headers of the form
``{quantity}_{unit}``. Every conforming file carries the required trio
``test_time_second``, ``voltage_volt`` and ``current_ampere``.

Sign convention (per the Battery Data Format specification, and used
throughout gleaned): **positive current charges the test object (current
flows into it); negative current discharges it.** Power follows the same
sign as current.
"""

from __future__ import annotations

import csv
import datetime
import json
import logging
from datetime import timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, TextIO

from ..protocols import SampleValue

__all__ = ["REQUIRED_COLUMNS", "BdfCsvSink", "dataset_filename", "validate_file"]

logger = logging.getLogger(__name__)

#: The trio every BDF file must contain, in the order they lead the header.
REQUIRED_COLUMNS: tuple[str, ...] = ("test_time_second", "voltage_volt", "current_ampere")

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

    On :meth:`close`, a sidecar ``<name>.meta.json`` (the ``.bdf.csv``
    suffix replaced) is written next to the data file, containing the
    ``metadata`` mapping plus the started/finished timestamps, the gleaned
    version, the column list and the row count.

    Args:
        path: Output file path, conventionally named via
            :func:`dataset_filename`.
        columns: Optional explicit column set (order-insensitive; the header
            ordering rule above is applied regardless).
        metadata: Optional JSON-serialisable mapping recorded in the sidecar
            (operator, cell id, instrument settings, ...).
    """

    def __init__(
        self,
        path: str | Path,
        *,
        columns: Sequence[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self._path = Path(path)
        self._explicit_columns = list(columns) if columns is not None else None
        self._metadata = dict(metadata or {})
        self._started_at = self._utcnow()
        self._columns: list[str] | None = None
        self._file: TextIO | None = None
        self._writer: csv.DictWriter | None = None
        self._rows_written = 0
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
        self._columns = _ordered_columns(inferred)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(
            self._file, fieldnames=self._columns, restval="", extrasaction="ignore"
        )
        self._writer.writeheader()
        logger.info("Writing BDF CSV %s with columns %s", self._path, self._columns)

    def write(self, rows: Iterable[Mapping[str, SampleValue]]) -> None:
        """Append a batch of samples to the file (opens it on first use)."""
        if self._closed:
            raise ValueError(f"BdfCsvSink for {self._path} is closed")
        batch = [dict(row) for row in rows]
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

    def close(self) -> None:
        """Close the CSV file and write the ``.meta.json`` sidecar. Idempotent."""
        if self._closed:
            return
        if self._writer is None:
            # Nothing was written: still emit a valid, empty BDF file.
            self._open([])
        assert self._file is not None
        self._file.close()
        self._file = None
        self._closed = True
        self._write_sidecar()

    def _sidecar_path(self) -> Path:
        name = self._path.name
        if name.endswith(_BDF_SUFFIX):
            stem = name[: -len(_BDF_SUFFIX)]
        else:
            stem = self._path.stem
        return self._path.with_name(stem + ".meta.json")

    def _write_sidecar(self) -> None:
        from gleaned import __version__  # local import to avoid a cycle at module load

        sidecar = {
            "file": self._path.name,
            "metadata": self._metadata,
            "started_at": self._started_at,
            "finished_at": self._utcnow(),
            "gleaned_version": __version__,
            "columns": self._columns or [],
            "rows": self._rows_written,
        }
        path = self._sidecar_path()
        path.write_text(json.dumps(sidecar, indent=2, default=str) + "\n", encoding="utf-8")
        logger.info("Wrote sidecar %s", path)


def validate_file(path: str | Path) -> dict[str, Any]:
    """Validate an emitted file with the ``batterydf`` package (optional extra).

    Returns the validation report dict from ``bdf.validate`` (it contains at
    least an ``"ok"`` boolean). gleaned itself never parses or normalises
    vendor data; this simply hands the finished file to the reference
    implementation of the format.

    Raises:
        ImportError: if ``batterydf`` is not installed -- install it with
            ``pip install "gleaned[bdf]"``.
        RuntimeError: if ``batterydf`` fails while validating the file.
    """
    try:
        import bdf  # the import name of the 'batterydf' distribution
    except ImportError as exc:
        raise ImportError(
            "validate_file needs the optional 'batterydf' package, which is "
            'not installed. Install it with: pip install "gleaned[bdf]".'
        ) from exc
    try:
        report = bdf.validate(str(path))
    except Exception as exc:
        raise RuntimeError(f"batterydf failed to validate {path}: {exc}") from exc
    if not report.get("ok", False):
        logger.warning("batterydf reports %s as not OK: %s", path, report)
    return report
