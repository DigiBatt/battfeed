"""Polling loop that moves samples from a :class:`DataSource` into a :class:`Sink`.

The :class:`Harvester` owns no I/O of its own: sources produce samples,
sinks persist them, and the harvester just runs the clock. The clock and
sleep functions are injectable so the loop can be tested (and simulated)
without waiting on wall time.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .protocols import DataSource, Sink

__all__ = ["CollectStats", "Harvester"]

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class CollectStats:
    """Summary of one :meth:`Harvester.collect` run."""

    samples: int
    """Total number of samples written to the sink."""

    duration_s: float
    """Elapsed collection time in seconds (measured with the injected clock)."""

    started_at: str
    """Wall-clock start of the run as an ISO 8601 UTC timestamp."""

    source: str
    """Name of the source that was collected."""

    columns: list[str] = field(default_factory=list)
    """Sorted union of the column names seen across all samples."""


class Harvester:
    """Registers named sources and runs timed collection loops against them.

    Typical use::

        harvester = Harvester()
        harvester.register(SimulatedCellSource())
        sink = BdfCsvSink("LOCAL__DemoCell__20260707_001.bdf.csv")
        stats = harvester.collect("simulator", duration_s=60, interval_s=1.0, sink=sink)
        sink.close()

    The harvester never closes the sink and never closes the source: their
    owner (your script, or the gleaned CLI) does. This keeps repeated
    collections against the same source or sink possible.
    """

    def __init__(self) -> None:
        self._sources: dict[str, DataSource] = {}
        self._status: dict[str, dict[str, Any]] = {}

    def register(self, source: DataSource) -> None:
        """Register ``source`` under its ``name``. Re-registering replaces it."""
        name = source.name
        if name in self._sources:
            logger.warning("Replacing already-registered source %r", name)
        self._sources[name] = source
        self._status.setdefault(name, {"last_poll_at": None, "samples_collected": 0})
        logger.info("Registered source %r", name)

    @property
    def sources(self) -> dict[str, DataSource]:
        """Mapping of registered source names to source objects (a copy)."""
        return dict(self._sources)

    def status(self, source_name: str) -> dict[str, Any]:
        """Return a status snapshot for ``source_name``.

        The dict always contains ``registered`` (bool), ``last_poll_at``
        (ISO 8601 string or ``None``) and ``samples_collected`` (int, total
        across all collect runs in this harvester's lifetime).
        """
        state = self._status.get(source_name, {})
        return {
            "registered": source_name in self._sources,
            "last_poll_at": state.get("last_poll_at"),
            "samples_collected": state.get("samples_collected", 0),
        }

    def collect(
        self,
        source_name: str,
        *,
        duration_s: float,
        interval_s: float = 1.0,
        sink: Sink,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        stop: threading.Event | None = None,
    ) -> CollectStats:
        """Poll ``source_name`` every ``interval_s`` for ``duration_s`` seconds.

        Each iteration: poll the source, stamp ``test_time_second`` (elapsed
        time from the injected ``clock``) onto samples that lack it, hand the
        batch to ``sink.write``, then sleep until the next tick. The loop
        ends when ``duration_s`` has elapsed or ``stop`` is set, whichever
        comes first.

        ``clock`` and ``sleep`` exist so tests can drive the loop with fake
        time; production callers keep the defaults.

        Raises:
            KeyError: if ``source_name`` is not registered.
        """
        try:
            source = self._sources[source_name]
        except KeyError:
            raise KeyError(
                f"No source registered under {source_name!r}. "
                f"Registered sources: {sorted(self._sources) or 'none'}"
            ) from None
        if interval_s <= 0:
            raise ValueError(f"interval_s must be positive, got {interval_s}")

        state = self._status[source_name]
        started_at = _utcnow_iso()
        columns: set[str] = set()
        samples = 0
        start = clock()
        logger.info(
            "Collecting from %r for %.3gs at %.3gs intervals", source_name, duration_s, interval_s
        )

        while (clock() - start) < duration_s and not (stop is not None and stop.is_set()):
            batch = [dict(row) for row in source.poll()]
            elapsed = clock() - start
            for row in batch:
                row.setdefault("test_time_second", elapsed)
                columns.update(row)
            if batch:
                sink.write(batch)
                samples += len(batch)
            state["last_poll_at"] = _utcnow_iso()
            state["samples_collected"] += len(batch)
            logger.debug("Poll of %r returned %d sample(s)", source_name, len(batch))

            remaining = duration_s - (clock() - start)
            if remaining <= 0 or (stop is not None and stop.is_set()):
                break
            sleep(min(interval_s, remaining))

        stats = CollectStats(
            samples=samples,
            duration_s=clock() - start,
            started_at=started_at,
            source=source_name,
            columns=sorted(columns),
        )
        logger.info(
            "Collected %d sample(s) from %r in %.3fs", stats.samples, source_name, stats.duration_s
        )
        return stats
