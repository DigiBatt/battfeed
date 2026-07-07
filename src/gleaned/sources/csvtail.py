"""Tail a growing CSV file (e.g. an instrument's live log) as a data source."""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any, Mapping

__all__ = ["CsvTailSource"]

logger = logging.getLogger(__name__)


class CsvTailSource:
    """Follow a CSV file that another process is appending to.

    On each :meth:`poll`, rows appended since the last poll are read, their
    headers renamed to canonical BDF column names via ``column_map``, and
    their values optionally rescaled via ``unit_scale``. Only complete lines
    (terminated by a newline) are consumed, so a row the writer is midway
    through appending is picked up on the next poll instead of being
    half-read.

    This source is **deliberately config-driven, not clever**: you state
    exactly which source column becomes which BDF column and what factor
    converts its unit. It is not a synonym-guessing or vendor-format engine
    -- normalising exported vendor files (Neware, BioLogic, Digatron, ...)
    is the job of the ``batterydf`` package (Battery Data Alliance), not of
    gleaned.

    Args:
        path: CSV file to follow. May not exist yet; polls return no samples
            until it does.
        column_map: Mapping of *source header* -> *canonical BDF column
            name*, e.g. ``{"Volts": "voltage_volt", "Amps": "current_ampere"}``.
            Source columns not listed are dropped. Map the source's own time
            column to ``test_time_second`` to preserve the instrument
            timebase; otherwise the harvester stamps elapsed time.
        unit_scale: Optional mapping of *canonical BDF column name* ->
            multiplicative factor applied after renaming. Example: a source
            logging millivolt uses ``{"voltage_volt": 0.001}``.
        name: Source name, default ``"csvtail"``.

    Limitations: one record per line (no newlines inside quoted fields),
    text encoding fixed at construction (default UTF-8), values must parse
    as floats (rows with unparseable mapped values are skipped with a
    warning).
    """

    def __init__(
        self,
        path: str | Path,
        column_map: Mapping[str, str],
        unit_scale: Mapping[str, float] | None = None,
        name: str = "csvtail",
        *,
        encoding: str = "utf-8",
    ) -> None:
        if not column_map:
            raise ValueError("column_map must map at least one source column to a BDF name")
        self.name = name
        self._path = Path(path)
        self._column_map = dict(column_map)
        self._unit_scale = dict(unit_scale or {})
        self._encoding = encoding
        self._offset = 0  # byte offset of the first unconsumed line
        self._header: list[str] | None = None

    def metadata(self) -> Mapping[str, Any]:
        return {
            "source": self.name,
            "kind": "csv-tail",
            "path": str(self._path),
            "column_map": dict(self._column_map),
            "unit_scale": dict(self._unit_scale),
        }

    def poll(self) -> list[dict[str, float]]:
        """Return the rows appended (as complete lines) since the last poll."""
        try:
            with open(self._path, "rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
        except FileNotFoundError:
            return []
        if not chunk:
            return []

        last_newline = chunk.rfind(b"\n")
        if last_newline < 0:
            return []  # no complete new line yet; try again next poll
        complete = chunk[: last_newline + 1]
        self._offset += len(complete)

        samples: list[dict[str, float]] = []
        for record in csv.reader(complete.decode(self._encoding, errors="replace").splitlines()):
            if not record:
                continue
            if self._header is None:  # first complete line of the file is the header
                self._header = [cell.strip() for cell in record]
                continue
            sample = self._map_record(dict(zip(self._header, record)))
            if sample:
                samples.append(sample)
        return samples

    def _map_record(self, raw: dict[str, str]) -> dict[str, float] | None:
        sample: dict[str, float] = {}
        for source_column, bdf_name in self._column_map.items():
            value = raw.get(source_column, "").strip()
            if not value:
                continue
            try:
                number = float(value)
            except ValueError:
                logger.warning(
                    "Skipping row in %s: column %r value %r is not numeric",
                    self._path, source_column, value,
                )
                return None
            sample[bdf_name] = number * self._unit_scale.get(bdf_name, 1.0)
        return sample or None
