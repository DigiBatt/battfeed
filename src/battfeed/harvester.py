"""Polling loop that moves samples from a :class:`DataSource` into a :class:`Sink`.

The :class:`Harvester` owns no I/O of its own: sources produce samples,
sinks persist them, and the harvester just runs the clock. The clock and
sleep functions are injectable so the loop can be tested (and simulated)
without waiting on wall time.

Resilience: field collection has to survive flaky hardware. A transient
``poll()`` failure does not abort the run -- the harvester retries with
exponential backoff under a configurable :class:`ErrorPolicy` and only
gives up (raising :class:`SourceFailure`) after too many *consecutive*
failures. Sources therefore stay simple: raise on trouble, reconnect on
the next poll.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .protocols import RESERVED_KEYS, DataSource, Sink

__all__ = ["CollectStats", "ErrorPolicy", "Harvester", "SourceFailure"]

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ErrorPolicy:
    """How :meth:`Harvester.collect` treats ``poll()`` failures.

    A failed poll is logged and retried after an exponentially growing
    delay; any successful poll resets the consecutive-failure counter.
    Once ``max_consecutive_errors`` failures occur in a row the run is
    abandoned with :class:`SourceFailure`.
    """

    max_consecutive_errors: int = 5
    backoff_initial_s: float = 1.0
    backoff_factor: float = 2.0
    backoff_max_s: float = 30.0

    def backoff(self, consecutive: int) -> float:
        """Delay before the next attempt after ``consecutive`` (>=1) failures."""
        return min(
            self.backoff_initial_s * self.backoff_factor ** (consecutive - 1),
            self.backoff_max_s,
        )


class SourceFailure(RuntimeError):
    """A source kept failing beyond its :class:`ErrorPolicy` allowance."""

    def __init__(self, source: str, consecutive: int, last_error: Exception) -> None:
        super().__init__(
            f"Source {source!r} failed {consecutive} times in a row; giving up. "
            f"Last error: {last_error!r}"
        )
        self.source = source
        self.consecutive = consecutive
        self.last_error = last_error


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

    errors: int = 0
    """Number of tolerated (retried) poll failures during the run."""


class Harvester:
    """Registers named sources and runs timed collection loops against them.

    Typical use::

        harvester = Harvester()
        harvester.register(SimulatedCellSource())
        sink = BdfCsvSink("LOCAL__DemoCell__20260707_001.bdf.csv")
        stats = harvester.collect("simulator", duration_s=60, interval_s=1.0, sink=sink)
        sink.close()

    The harvester never closes the sink and never closes the source: their
    owner (your script, or the battfeed CLI) does. This keeps repeated
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
        duration_s: float | None = None,
        interval_s: float = 1.0,
        sink: Sink,
        errors: ErrorPolicy | None = ErrorPolicy(),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        stop: threading.Event | None = None,
    ) -> CollectStats:
        """Poll ``source_name`` every ``interval_s`` seconds into ``sink``.

        With ``duration_s=None`` (the default) the loop runs until ``stop``
        is set -- the mode field collectors use; pass a number of seconds
        for a bounded run. Each iteration: poll the source, stamp
        ``test_time_second`` (elapsed time from the injected ``clock``) onto
        samples that lack it, hand the batch to ``sink.write``, then sleep
        until the next tick.

        ``poll()`` exceptions are governed by ``errors``: transient failures
        are logged and retried with exponential backoff, and only
        ``errors.max_consecutive_errors`` failures in a row abandon the run
        with :class:`SourceFailure`. Pass ``errors=None`` to fail fast on
        the first exception instead.

        ``clock`` and ``sleep`` exist so tests can drive the loop with fake
        time; production callers keep the defaults.

        Raises:
            KeyError: if ``source_name`` is not registered.
            ValueError: if ``interval_s`` is not positive, or neither
                ``duration_s`` nor ``stop`` is provided (which would loop
                forever with no way to end).
            SourceFailure: if the source exceeds its error allowance.
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
        if duration_s is None and stop is None:
            raise ValueError("collect() with duration_s=None needs a stop event to be stoppable")

        state = self._status[source_name]
        started_at = _utcnow_iso()
        columns: set[str] = set()
        samples = 0
        error_count = 0
        consecutive_errors = 0
        warned_i5 = False
        start = clock()
        logger.info(
            "Collecting from %r %s at %.3gs intervals",
            source_name,
            "until stopped" if duration_s is None else f"for {duration_s:.3g}s",
            interval_s,
        )

        def stopped() -> bool:
            return stop is not None and stop.is_set()

        def remaining() -> float | None:
            return None if duration_s is None else duration_s - (clock() - start)

        def bounded_sleep(delay: float) -> None:
            left = remaining()
            sleep(delay if left is None else max(0.0, min(delay, left)))

        while not stopped() and (duration_s is None or (clock() - start) < duration_s):
            try:
                batch = [dict(row) for row in source.poll()]
            except Exception as exc:  # noqa: BLE001 -- sources are third-party code
                if errors is None:
                    raise
                error_count += 1
                consecutive_errors += 1
                if consecutive_errors >= errors.max_consecutive_errors:
                    raise SourceFailure(source_name, consecutive_errors, exc) from exc
                delay = errors.backoff(consecutive_errors)
                logger.warning(
                    "Poll of %r failed (%d consecutive); retrying in %.3gs: %s",
                    source_name,
                    consecutive_errors,
                    delay,
                    exc,
                )
                bounded_sleep(delay)
                continue

            consecutive_errors = 0
            elapsed = clock() - start
            for row in batch:
                if "test_time_second" not in row:
                    # Stamping is only correct for single-object sources: a
                    # routed row getting the SHARED elapsed-collection time is
                    # a contract violation (invariant I5) -- an object that
                    # appears mid-run would not start its file at t = 0.
                    if not warned_i5 and any(key in row for key in RESERVED_KEYS):
                        warned_i5 = True
                        logger.warning(
                            "Source %r emits routing keys (%s) without supplying its own "
                            "test_time_second; stamping the shared elapsed-collection time "
                            "violates the routing contract (invariant I5) and yields a wrong "
                            "timebase for multi-object streams",
                            source_name,
                            "/".join(RESERVED_KEYS),
                        )
                    row["test_time_second"] = elapsed
                columns.update(row)
            if batch:
                sink.write(batch)
                samples += len(batch)
            state["last_poll_at"] = _utcnow_iso()
            state["samples_collected"] += len(batch)
            logger.debug("Poll of %r returned %d sample(s)", source_name, len(batch))

            left = remaining()
            if (left is not None and left <= 0) or stopped():
                break
            bounded_sleep(interval_s)

        stats = CollectStats(
            samples=samples,
            duration_s=clock() - start,
            started_at=started_at,
            source=source_name,
            columns=sorted(columns),
            errors=error_count,
        )
        logger.info(
            "Collected %d sample(s) from %r in %.3fs (%d tolerated error(s))",
            stats.samples,
            source_name,
            stats.duration_s,
            stats.errors,
        )
        return stats
