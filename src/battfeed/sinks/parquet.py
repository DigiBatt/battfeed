"""Write collected samples as a single Parquet file (optional extra).

Parquet is an *analytics* format, not BDF: the reserved routing keys
(``series_id`` / ``run_id``) are kept as ordinary columns so downstream
dataframe work can group by object and run -- in deliberate contrast to
:class:`battfeed.BdfCsvSink`, which must strip them.

Deliberately simple: rows are buffered in memory and one file is written
on :meth:`ParquetSink.close` -- no row-group tuning, no append mode, no
rotation. Long-running rotation belongs to ``RoutingSink`` + BDF files;
Parquet is for bounded analytical captures.

Requires the optional ``pyarrow`` dependency::

    pip install "battfeed[parquet]"
"""

from __future__ import annotations

import datetime
import importlib
import json
import logging
import os
from datetime import timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable, Mapping

from ..protocols import SampleValue
from .http_push import _serialize_row

__all__ = ["ParquetSink"]

logger = logging.getLogger(__name__)

_PARQUET_SUFFIX = ".parquet"


def _import_pyarrow() -> tuple[ModuleType, ModuleType]:
    try:
        pa = importlib.import_module("pyarrow")
        pq = importlib.import_module("pyarrow.parquet")
    except ImportError as exc:
        raise ImportError(
            "ParquetSink needs the optional 'pyarrow' package, which is not "
            'installed. Install it with: pip install "battfeed[parquet]".'
        ) from exc
    return pa, pq


class ParquetSink:
    """Buffer samples in memory and write one Parquet file on close.

    The column set is the union of the keys of every row written, in
    first-seen order; rows missing a column get a null in that cell. The
    reserved routing keys (``series_id`` / ``run_id``) are **included** as
    ordinary columns -- Parquet is an analytics format, not BDF.

    A ``<name>.meta.json`` sidecar is written next to the data file in the
    same format :class:`battfeed.BdfCsvSink` uses (metadata, started/finished
    timestamps, battfeed version, columns, row count, ``finalized: true``).
    Because everything is written at close, there is no early/unfinalised
    sidecar stage: this sink is for bounded analytical captures, not
    unbounded telemetry -- use ``RoutingSink`` + BDF for rotation.

    Args:
        path: Output ``.parquet`` file path.
        metadata: Optional JSON-serialisable mapping recorded in the sidecar.

    Raises:
        ImportError: on construction, if ``pyarrow`` is not installed.
    """

    def __init__(self, path: str | Path, *, metadata: Mapping[str, Any] | None = None) -> None:
        # Fail fast: better to learn pyarrow is missing at construction than
        # after buffering an entire capture.
        self._pa, self._pq = _import_pyarrow()
        self._path = Path(path)
        self._metadata = dict(metadata or {})
        self._started_at = self._utcnow()
        self._rows: list[dict[str, SampleValue]] = []
        self._columns: list[str] = []
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def _utcnow() -> str:
        return datetime.datetime.now(timezone.utc).isoformat(timespec="seconds")

    def write(self, rows: Iterable[Mapping[str, SampleValue]]) -> None:
        """Buffer a batch of samples (nothing touches disk until close)."""
        if self._closed:
            raise ValueError(f"ParquetSink for {self._path} is closed")
        self._rows.extend(dict(row) for row in rows)

    def close(self) -> None:
        """Write the Parquet file and its ``.meta.json`` sidecar. Idempotent.

        Never raises and never loses the capture: if the table build or the
        file write fails (e.g. ``ArrowInvalid`` from mixed-type columns),
        every buffered row is rescued to ``<stem>.rescue.ndjson`` beside the
        target (one RFC-valid JSON object per line, non-finite floats as
        null) and the sidecar is written with ``finalized: false`` plus an
        ``error`` field naming the failure. The data file itself is written
        via a temporary file and ``os.replace``, so a failed write leaves no
        partial file at the final path.
        """
        if self._closed:
            return
        self._closed = True
        # Union of keys in first-seen order; missing values become nulls.
        self._columns = list(dict.fromkeys(key for row in self._rows for key in row))
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            data = {column: [row.get(column) for row in self._rows] for column in self._columns}
            table = self._pa.table(data)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._pq.write_table(table, tmp)
            os.replace(tmp, self._path)
        except Exception as exc:  # ArrowInvalid, OSError, ... -- rescue, never raise
            self._rescue(exc, tmp)
            return
        logger.info(
            "Wrote %d rows x %d columns to %s", len(self._rows), len(self._columns), self._path
        )
        self._write_sidecar(finalized=True)

    def _rescue(self, exc: Exception, tmp: Path) -> None:
        """Dump every buffered row to an NDJSON rescue file; never raise."""
        try:
            tmp.unlink(missing_ok=True)  # no partial file left behind
        except OSError:
            pass
        error = f"{type(exc).__name__}: {exc}"
        try:
            rescue = self._path.with_name(self._stem() + ".rescue.ndjson")
            lines = [_serialize_row(row)[0] for row in self._rows]
            rescue.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
            logger.error(
                "Could not write parquet file %s (%s); rescued all %d buffered rows to %s",
                self._path,
                error,
                len(self._rows),
                rescue,
            )
            self._write_sidecar(finalized=False, error=error)
        except OSError as rescue_exc:
            logger.error(
                "Could not write parquet file %s (%s) AND the rescue write failed (%s); "
                "%d rows remain only in memory",
                self._path,
                error,
                rescue_exc,
                len(self._rows),
            )

    def _stem(self) -> str:
        name = self._path.name
        if name.endswith(_PARQUET_SUFFIX):
            return name[: -len(_PARQUET_SUFFIX)]
        return self._path.stem

    def _sidecar_path(self) -> Path:
        return self._path.with_name(self._stem() + ".meta.json")

    def _write_sidecar(self, *, finalized: bool, error: str | None = None) -> None:
        from battfeed import __version__  # local import to avoid a cycle at module load

        sidecar = {
            "file": self._path.name,
            "metadata": self._metadata,
            "started_at": self._started_at,
            "finished_at": self._utcnow(),
            "battfeed_version": __version__,
            "columns": self._columns,
            "rows": len(self._rows),
            "finalized": finalized,
        }
        if error is not None:
            sidecar["error"] = error
        path = self._sidecar_path()
        # Write-then-replace so a crash cannot leave a half-written sidecar.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(sidecar, indent=2, default=str) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        logger.info("Wrote sidecar %s", path)
