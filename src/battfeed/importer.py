"""Batch-import driver: poll a file-ingesting source to exhaustion, or forever.

Importers are ordinary :class:`~battfeed.DataSource`\\ s (invariant I3 -- one
seam, no parallel pipeline): a folder-watching source parses the *next*
un-ingested file per ``poll()`` and returns its rows as one batch. What a
batch import needs that live collection does not is a different *loop*: not
"poll every N seconds for a duration" but "poll until there is nothing left"
(one-shot) or "poll forever, idling between checks for new files" (watch).
:func:`run_import` is that loop -- a thin driver over the same ``poll()``
contract, nothing more.

Drained detection
-----------------
One-shot mode stops when the source reports drained. A source MAY implement
the optional hook ``drained() -> bool``; it is consulted after every *empty*
poll, so a source that knows more files are pending (e.g. it quarantined one
and wants another look, or ingestion is deliberately paced) can return
``False`` to keep the driver polling. A source without the hook is considered
drained after its first empty poll. Because a ``drained()`` hook can keep a
one-shot run polling indefinitely, one-shot mode on a hook-bearing source
requires a ``stop`` event, exactly like watch mode (the CLI always passes
one).

Commit point and at-least-once delivery
---------------------------------------
A batch source keeping durable dedupe state (see
:class:`battfeed.ingest_state.ImportLedger`) must NOT mark a file as ingested
inside ``poll()`` -- at that moment its rows exist only in memory, and a crash
or sink failure before the write completes would lose the file forever while
the ledger swears it was imported. The commit point lives *after* the write:
when ``sink.write(batch)`` returns successfully, the driver calls the
optional source hook ``commit_batch() -> None``, and THAT is where the source
records the batch's file in its ledger. The resulting semantics are
**at-least-once**: a crash between poll and commit leaves the file
un-recorded, so the next run imports it again. Duplicates from such a re-run
land in NEW segment files (never overwriting the earlier ones) because
:class:`~battfeed.RoutingSink` reserves every output path atomically --
re-imported data is a visible, de-duplicable artifact, not silent corruption.
The previous design (record inside ``poll()``) was silently at-most-once:
a crash in the poll-to-write window lost the file permanently.

Error handling
--------------
Sources raise on trouble; the driver owns retry (invariant I4). Rather than
duplicating backoff logic, the driver reuses the harvester's
:class:`~battfeed.ErrorPolicy` -- the same consecutive-failure accounting and
exponential backoff, abandoning the run with :class:`~battfeed.SourceFailure`
only after ``max_consecutive_errors`` failures in a row.

Timebase ownership (invariant I5)
---------------------------------
Unlike :meth:`Harvester.collect`, this driver **never stamps**
``test_time_second``: imported files carry their own timebase and their rows
carry routing keys (imported data is inherently multi-(series, run)), so the
source must supply ``test_time_second`` zero-based per (series, run). Rows
are passed to the sink exactly as the source produced them.

The driver never closes the source or the sink: their owner (your script, or
the ``battfeed import`` CLI verb) does -- mirroring the harvester.

CLI hook: ``reset_ledger()``
----------------------------
Batch sources keep durable dedupe state (see
:class:`battfeed.ingest_state.ImportLedger`) at a location of their own
choosing, so the CLI cannot clear it directly. A source that keeps a ledger
SHOULD expose an optional ``reset_ledger() -> None`` hook; ``battfeed import
--reset-ledger`` calls it (and fails loudly on sources that lack it).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .harvester import ErrorPolicy, SourceFailure
from .protocols import DataSource, Sink

__all__ = ["ImportStats", "run_import"]

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ImportStats:
    """Summary of one :func:`run_import` run."""

    samples: int
    """Total number of samples written to the sink."""

    polls: int
    """Number of successful ``poll()`` calls (failed polls are ``errors``)."""

    batches: int
    """Number of non-empty batches written to the sink."""

    duration_s: float
    """Elapsed driver time in seconds (measured with the injected clock)."""

    started_at: str
    """Wall-clock start of the run as an ISO 8601 UTC timestamp."""

    source: str
    """Name of the source that was drained/watched."""

    columns: list[str] = field(default_factory=list)
    """Sorted union of the column names seen across all samples."""

    errors: int = 0
    """Number of tolerated (retried) poll failures during the run."""


def _is_drained(source: DataSource) -> bool:
    """Consult the optional ``drained()`` hook; absent hook = drained."""
    hook = getattr(source, "drained", None)
    if callable(hook):
        return bool(hook())
    return True


def run_import(
    source: DataSource,
    sink: Sink,
    *,
    watch: bool = False,
    interval_s: float = 5.0,
    stop: threading.Event | None = None,
    errors: ErrorPolicy | None = ErrorPolicy(),
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] | None = None,
) -> ImportStats:
    """Poll ``source`` and write its batches to ``sink`` until drained (or stopped).

    Each successful non-empty poll is written to the sink and followed
    *immediately* by the next poll -- a source that keeps returning rows is
    drained at full speed, with no sleeps at all. Only an empty poll idles:
    in watch mode the driver waits ``interval_s`` and polls again, forever,
    until ``stop`` is set; in one-shot mode (the default) an empty poll ends
    the run once the source reports drained (see the module docstring for the
    optional ``drained()`` hook).

    After each successful ``sink.write(batch)`` the driver calls the source's
    optional ``commit_batch()`` hook -- the commit point where a batch source
    records the ingested file in its ledger (see the module docstring:
    at-least-once, never silently at-most-once). A ``sink.write`` failure
    propagates immediately (it is not retried by ``errors``, which governs
    ``poll()`` only) and the un-committed batch is re-imported on the next
    run.

    ``poll()`` exceptions are governed by ``errors`` exactly as in
    :meth:`Harvester.collect`: transient failures are logged and retried with
    exponential backoff, and only ``errors.max_consecutive_errors`` failures
    in a row abandon the run with :class:`SourceFailure`. Pass ``errors=None``
    to fail fast on the first exception instead.

    ``stop`` is honoured in both modes: it is checked between polls, and when
    ``sleep`` is left at its default every wait (idle interval and error
    backoff alike) is ``stop.wait``, so setting the event interrupts even a
    long backoff immediately. Pass ``clock`` and ``sleep`` to drive the loop
    without wall time in tests (an explicit ``sleep`` is used verbatim and is
    then responsible for its own stop-responsiveness).

    Rows are BDF-validity-checked only for the one thing the driver can see:
    a batch source owns its per-(series, run) timebase (invariant I5), so the
    first batch containing rows without ``test_time_second`` draws a single
    warning per run naming the source.

    Raises:
        ValueError: if ``interval_s`` is not positive, or ``watch=True``
            without a ``stop`` event, or one-shot mode on a source with a
            ``drained()`` hook without a ``stop`` event (either could loop
            forever with no way to end).
        SourceFailure: if the source exceeds its error allowance.
    """
    if interval_s <= 0:
        raise ValueError(f"interval_s must be positive, got {interval_s}")
    if watch and stop is None:
        raise ValueError("run_import(watch=True) needs a stop event to be stoppable")
    if not watch and stop is None and callable(getattr(source, "drained", None)):
        raise ValueError(
            f"run_import() on source {source.name!r} needs a stop event to be "
            "stoppable: its drained() hook may keep a one-shot run polling "
            "indefinitely"
        )

    # All waits go through `wait`: an explicitly injected sleep wins (tests own
    # the clock), otherwise stop.wait so a stop request interrupts even a long
    # error backoff immediately, otherwise plain time.sleep (stop-less runs).
    wait: Callable[[float], object]
    if sleep is not None:
        wait = sleep
    elif stop is not None:
        wait = stop.wait
    else:
        wait = time.sleep

    commit = getattr(source, "commit_batch", None)
    if not callable(commit):
        commit = None
    started_at = _utcnow_iso()
    columns: set[str] = set()
    samples = 0
    polls = 0
    batches = 0
    error_count = 0
    consecutive_errors = 0
    warned_missing_time = False
    start = clock()
    logger.info(
        "Importing from %r (%s, %.3gs interval between checks)",
        source.name,
        "watching until stopped" if watch else "one-shot until drained",
        interval_s,
    )

    def stopped() -> bool:
        return stop is not None and stop.is_set()

    while not stopped():
        try:
            batch = [dict(row) for row in source.poll()]
        except Exception as exc:  # noqa: BLE001 -- sources are third-party code
            if errors is None:
                raise
            error_count += 1
            consecutive_errors += 1
            if consecutive_errors >= errors.max_consecutive_errors:
                raise SourceFailure(source.name, consecutive_errors, exc) from exc
            delay = errors.backoff(consecutive_errors)
            logger.warning(
                "Import poll of %r failed (%d consecutive); retrying in %.3gs: %s",
                source.name,
                consecutive_errors,
                delay,
                exc,
            )
            wait(delay)
            continue

        consecutive_errors = 0
        polls += 1
        if batch:
            if not warned_missing_time and any("test_time_second" not in row for row in batch):
                # Imported rows are never time-stamped by the driver: a batch
                # source owns its per-(series, run) timebase (invariant I5),
                # and rows without test_time_second produce invalid BDF.
                warned_missing_time = True
                logger.warning(
                    "Source %r emitted imported rows without test_time_second; a batch "
                    "import source must supply its own zero-based-per-(series, run) "
                    "timebase (invariant I5) -- rows without it produce invalid BDF",
                    source.name,
                )
            for row in batch:
                columns.update(row)
            sink.write(batch)
            samples += len(batch)
            batches += 1
            if commit is not None:
                # The batch is safely in the sink: NOW the source may record
                # its file as ingested (at-least-once; see module docstring).
                commit()
            logger.debug("Import poll of %r returned %d sample(s)", source.name, len(batch))
            continue  # drain the backlog immediately; only empty polls idle

        # Empty poll: the source found nothing new to ingest right now.
        if not watch and _is_drained(source):
            break
        if stopped():
            break
        wait(interval_s)

    stats = ImportStats(
        samples=samples,
        polls=polls,
        batches=batches,
        duration_s=clock() - start,
        started_at=started_at,
        source=source.name,
        columns=sorted(columns),
        errors=error_count,
    )
    logger.info(
        "Imported %d sample(s) in %d batch(es) from %r in %.3fs (%d tolerated error(s))",
        stats.samples,
        stats.batches,
        source.name,
        stats.duration_s,
        stats.errors,
    )
    return stats
