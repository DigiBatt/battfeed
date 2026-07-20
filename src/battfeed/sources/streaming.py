"""StreamingSource: adapt push-style delivery to the synchronous ``poll()`` seam.

Many integrations do not answer questions -- they *talk*: BLE notification
callbacks, CAN frames, MQTT messages. The battfeed contract, on the other
hand, is deliberately pull-based (:meth:`battfeed.DataSource.poll`), because
one synchronous seam keeps every source testable and every pipeline identical.
:class:`StreamingSource` bridges the two: a subclass supplies a blocking
:meth:`~StreamingSource.run_reader` loop, the base runs it in a daemon thread,
and readings accumulate in a bounded buffer that ``poll()`` drains.

Three design points carry the invariants and are worth spelling out:

* **The buffer is bounded, and overflow is counted (invariant I2 -- no silent
  data loss).** An unbounded buffer would grow without limit whenever the
  device outpaces the poll loop -- a months-long feed would fail by memory
  exhaustion at the worst possible moment. So the buffer is a
  ``deque(maxlen=buffer_size)``, which keeps the *newest* readings. A plain
  ``deque`` drops the oldest entry silently, though, which would be exactly
  the silent loss I2 forbids -- so every overflow increments
  :attr:`~StreamingSource.dropped_total`, is surfaced through
  :meth:`~StreamingSource.stream_stats` / ``metadata()``, and emits a
  rate-limited warning.

* **Reader errors surface at ``poll()``, and a dead device keeps FAILING
  (invariant I4 -- sources raise, the harvester owns retry).** A background
  thread that dies quietly would leave a source that "collects" nothing
  forever. An exception that escapes ``run_reader`` is parked and re-raised
  by the next ``poll()``; the poll after that starts a replacement reader
  session. The trap in that design is subtle: if the restarting poll simply
  returned ``[]`` it would count as a success, reset the harvester's
  consecutive-failure counter, and a permanently dead device would alternate
  failure with fabricated success forever -- ``SourceFailure`` unreachable,
  backoff pinned at its minimum. So restart polls are honest instead: while
  the previous session **ended without delivering a single sample**, the
  restarting poll starts the replacement *and raises*
  :class:`DeadReaderError` noting how many sessions in a row died empty.
  During a dead-device period every poll therefore raises, the harvester's
  counter genuinely accumulates with escalating backoff, and the default
  :class:`battfeed.ErrorPolicy` reaches :class:`battfeed.SourceFailure`. A
  session that delivers at least one sample resets the escalation, so a
  flaky-but-working device is retried indefinitely, exactly as intended. No
  retry loop belongs inside ``run_reader``.

* **Sessions have generations, so zombies cannot contaminate (I2 again, from
  the other side -- no *fabricated* data either).** ``close()`` joins the
  reader with a timeout; a reader stuck in a blocking wait that ignores
  ``should_stop`` is abandoned rather than blocking the caller forever.
  Every session is stamped with a generation number, and emits or death
  exceptions arriving from a stale generation are discarded (with a debug
  log) -- an abandoned zombie waking up later can neither inject samples
  into a newer session's stream nor fail it with a stale error.

The :class:`~battfeed.protocols.DataSource` protocol itself is unchanged --
third parties still need no battfeed import. Subclassing this base is merely
the convenient way to *satisfy* the protocol for push-style hardware; replay
fixtures for developing subclasses without hardware live in
:mod:`battfeed.testing.replay`.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Mapping

from ..protocols import Sample

__all__ = ["DeadReaderError", "StreamingSource"]

logger = logging.getLogger(__name__)

#: Minimum seconds between buffer-overflow warnings (per source instance).
_OVERFLOW_WARN_INTERVAL_S = 10.0


class DeadReaderError(RuntimeError):
    """The reader keeps ending without delivering a single sample.

    Raised by :meth:`StreamingSource.poll` *while it starts a replacement
    reader session*, whenever every session since the last delivered sample
    has died empty. This keeps a dead device failing at the harvester on
    every poll -- a restart that returned ``[]`` would be counted as a
    success and reset the :class:`battfeed.ErrorPolicy` consecutive-failure
    counter, making :class:`battfeed.SourceFailure` unreachable. A fresh
    instance is raised per restart; ``last_error`` carries the most recent
    exception a session died with (``None`` when sessions returned cleanly
    but empty).
    """

    def __init__(self, source: str, dead_sessions: int, last_error: Exception | None) -> None:
        detail = f"; last error: {last_error!r}" if last_error is not None else ""
        super().__init__(
            f"Reader of {source!r} ended {dead_sessions} consecutive time(s) without "
            f"delivering a sample; a replacement reader session was started{detail}"
        )
        self.source = source
        self.dead_sessions = dead_sessions
        self.last_error = last_error


class StreamingSource:
    """Base class for push-style sources; subclasses implement :meth:`run_reader`.

    The reader thread is started lazily on the first :meth:`poll` and runs
    :meth:`run_reader` until ``should_stop()`` turns True (set by
    :meth:`close`) or the reader raises. Emitted samples land in a bounded
    buffer that :meth:`poll` drains in arrival order. See the module
    docstring for why the buffer is bounded, why reader errors re-raise at
    ``poll()``, and why restarts after sample-less sessions raise
    :class:`DeadReaderError` instead of fabricating an empty success.

    Lifecycle: :meth:`close` is idempotent and *restartable* -- a later
    ``poll()`` starts a fresh reader session with a clean escalation history
    (mirroring ``Mc3000Source``, where a poll after ``close()`` reconnects).
    A reader that returns on its own (e.g. a scan that ended) is likewise
    restarted by the next poll -- silently when the session delivered
    samples, via :class:`DeadReaderError` when it ended empty. Every start
    after the first is counted in :attr:`reader_restarts`.

    Thread-safety: ``poll()`` and ``close()`` may be called from any thread
    (concurrent callers are serialized internally), and ``emit`` may be
    called from any reader-side callback.

    Args:
        name: Source name (the ``DataSource.name`` seam attribute).
        buffer_size: Maximum samples buffered between polls. When the reader
            outpaces the poll loop, the *oldest* buffered samples are dropped
            and counted in :attr:`dropped_total`.
        join_timeout_s: How long :meth:`close` waits for the reader thread to
            honor ``should_stop`` before abandoning it (the daemon thread
            cannot keep the process alive, and an abandoned session's late
            contributions are discarded by the generation guard).
        clock: Monotonic clock, injectable for tests (used only to rate-limit
            overflow warnings -- samples are never timestamped here; timebase
            policy belongs to the subclass or the harvester).
    """

    def __init__(
        self,
        name: str,
        *,
        buffer_size: int = 4096,
        join_timeout_s: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if buffer_size <= 0:
            raise ValueError(f"buffer_size must be positive, got {buffer_size}")
        self.name = name
        self._buffer_size = buffer_size
        self._join_timeout_s = join_timeout_s
        self._clock = clock
        #: Protects counters, buffer, generation and the parked exception.
        self._lock = threading.Lock()
        #: Serializes session start/stop bookkeeping (poll-restart vs close).
        #: Lock order: _start_lock BEFORE _lock, never the other way around.
        self._start_lock = threading.Lock()
        self._buffer: deque[Sample] = deque(maxlen=buffer_size)
        self._pending_exc: Exception | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._generation = 0
        self._gen_emits = 0
        self._dead_sessions = 0
        self._last_session_error: Exception | None = None
        self._starts = 0
        self._received_total = 0
        self._dropped_total = 0
        self._dropped_since_warn = 0
        self._last_overflow_warn: float | None = None

    # -- subclass seam -------------------------------------------------------
    def run_reader(self, emit: Callable[[Sample], None], should_stop: Callable[[], bool]) -> None:
        """Blocking receive loop; subclasses must override.

        Call ``emit(sample)`` for every received reading (thread-safe; may be
        called from callbacks) and return promptly once ``should_stop()`` is
        True -- check it between blocking waits, or use it to cancel them.
        Raise on trouble instead of retrying: the exception is re-raised by
        the next ``poll()`` and the harvester's error policy owns the retry
        (invariant I4). Raise a **fresh exception per crash** -- the base
        defensively clears a parked exception's ``__traceback__``, but a
        cached instance re-raised from several places accumulates state and
        confuses whoever reads the eventual stack trace.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement run_reader(emit, should_stop)"
        )

    # -- DataSource protocol -------------------------------------------------
    def metadata(self) -> Mapping[str, Any]:
        """Base metadata contribution: identity plus the streaming stats.

        Subclasses normally extend it: ``{**super().metadata(), ...}``. The
        stats ride along so every sidecar records whether the buffer ever
        overflowed (invariant I2).
        """
        return {
            "source": self.name,
            "kind": "streaming",
            "buffer_size": self._buffer_size,
            **self.stream_stats(),
        }

    def poll(self) -> list[Sample]:
        """Drain and return the buffered samples, in arrival order.

        If the reader session has died with an exception since the last poll,
        that exception is re-raised here (invariant I4) and no restart
        happens yet. The next poll starts a replacement session -- silently
        when the dead session had delivered at least one sample, otherwise
        that poll *also* raises :class:`DeadReaderError`, so a dead device
        fails on every poll and the harvester's error policy genuinely
        escalates (see the module docstring). The very first start never
        raises. Buffered samples survive raising polls and are returned once
        polling resumes.
        """
        with self._lock:
            exc, self._pending_exc = self._pending_exc, None
        if exc is not None:
            raise exc
        self._ensure_reader()
        with self._lock:
            drained = list(self._buffer)
            self._buffer.clear()
        return drained

    def close(self) -> None:
        """Stop the reader session. Idempotent; a later :meth:`poll` reconnects.

        Waits ``join_timeout_s`` for the reader to honor ``should_stop``,
        then abandons it: the generation guard discards anything a lingering
        zombie later emits or dies with. Closing also resets the
        dead-session escalation, so the post-close reconnect starts with a
        clean history (like the very first start).
        """
        with self._start_lock:
            self._stop.set()
            thread, self._thread = self._thread, None
            with self._lock:
                self._generation += 1  # anything the old session does now is stale
                self._dead_sessions = 0
                self._last_session_error = None
                exc, self._pending_exc = self._pending_exc, None
            if thread is not None:
                thread.join(timeout=self._join_timeout_s)
                if thread.is_alive():
                    logger.warning(
                        "Reader thread of %r ignored should_stop for %.3gs; abandoning it "
                        "(daemon thread; its late emits/errors will be discarded as stale)",
                        self.name,
                        self._join_timeout_s,
                    )
        if exc is not None:
            # Not silent: the error the reader died with is logged even though
            # no poll() will ever surface it now.
            logger.warning("Discarding pending reader error of %r on close: %r", self.name, exc)

    # -- stats ---------------------------------------------------------------
    @property
    def received_total(self) -> int:
        """Samples accepted from live reader sessions (including any later dropped)."""
        with self._lock:
            return self._received_total

    @property
    def dropped_total(self) -> int:
        """Samples lost to buffer overflow so far (oldest-first; invariant I2)."""
        with self._lock:
            return self._dropped_total

    @property
    def reader_restarts(self) -> int:
        """Reader session starts beyond the first (crash recoveries and reconnects)."""
        with self._lock:
            return max(0, self._starts - 1)

    @property
    def reader_alive(self) -> bool:
        """Best-effort: True while the *current* session's thread is running.

        A session abandoned by :meth:`close` (join timeout) may physically
        linger, but it no longer counts here and the generation guard keeps
        it from contributing anything.
        """
        thread = self._thread
        return thread is not None and thread.is_alive()

    def stream_stats(self) -> dict[str, int]:
        """One consistent snapshot of the counters (also merged into metadata)."""
        with self._lock:
            return {
                "received_total": self._received_total,
                "dropped_total": self._dropped_total,
                "reader_restarts": max(0, self._starts - 1),
            }

    # -- internals -----------------------------------------------------------
    def _emit(self, generation: int, sample: Sample) -> None:
        """Buffer one sample from session ``generation``. Thread-safe.

        Contributions from a stale generation (a zombie session abandoned by
        close(), or one already replaced after a crash) are discarded --
        logged at debug level, never counted, never buffered.
        """
        overflowed = 0
        with self._lock:
            if generation != self._generation:
                logger.debug(
                    "Discarding emit from stale reader session of %r (gen %d, current %d)",
                    self.name,
                    generation,
                    self._generation,
                )
                return
            if len(self._buffer) == self._buffer_size:
                # deque(maxlen=...) is about to evict the oldest entry
                # silently; count it so the loss is surfaced (invariant I2).
                self._dropped_total += 1
                self._dropped_since_warn += 1
                now = self._clock()
                if (
                    self._last_overflow_warn is None
                    or now - self._last_overflow_warn >= _OVERFLOW_WARN_INTERVAL_S
                ):
                    self._last_overflow_warn = now
                    overflowed, self._dropped_since_warn = self._dropped_since_warn, 0
            self._buffer.append(sample)
            self._received_total += 1
            self._gen_emits += 1
            self._dead_sessions = 0  # a delivering session resets the escalation
            dropped_total = self._dropped_total
        if overflowed:
            logger.warning(
                "Streaming buffer of %r overflowed: %d sample(s) dropped since the last "
                "warning (%d in total, oldest first). Poll more often or raise buffer_size.",
                self.name,
                overflowed,
                dropped_total,
            )

    def _ensure_reader(self) -> None:
        """Start a reader session if none is running (lazy first start, restarts).

        Serialized under ``_start_lock`` so concurrent polls cannot race the
        thread bookkeeping (e.g. joining a thread another caller has created
        but not yet started). When the previous session ended without having
        delivered a single sample, the replacement is started AND
        :class:`DeadReaderError` is raised -- see the module docstring.
        """
        with self._start_lock:
            thread = self._thread
            if thread is not None and thread.is_alive():
                return
            if thread is not None:
                thread.join(timeout=0)  # reap the finished thread
            stop = threading.Event()  # per-session: close() may have set the old one
            self._stop = stop
            with self._lock:
                self._generation += 1
                generation = self._generation
                self._gen_emits = 0
                self._starts += 1
                starts = self._starts
                dead_sessions = self._dead_sessions
                last_error = self._last_session_error
            new = threading.Thread(
                target=self._reader_main,
                args=(generation, stop),
                name=f"battfeed-{self.name}-reader",
                daemon=True,
            )
            self._thread = new
            if starts > 1:
                logger.info("Restarting reader of %r (start #%d)", self.name, starts)
            new.start()
            if starts > 1 and dead_sessions > 0:
                # Honest restart: returning [] here would register as a success
                # and reset the harvester's consecutive-failure counter, making
                # SourceFailure unreachable for a permanently dead device.
                raise DeadReaderError(self.name, dead_sessions, last_error) from last_error

    def _reader_main(self, generation: int, stop: threading.Event) -> None:
        """Thread body: run the subclass reader; park any exception for poll()."""

        def emit(sample: Sample) -> None:
            self._emit(generation, sample)

        try:
            self.run_reader(emit, stop.is_set)
        except Exception as exc:  # noqa: BLE001 -- readers are third-party code
            # Defensive traceback hygiene: a reader that re-raises a cached
            # exception instance would otherwise grow that instance's
            # traceback on every crash, and parked frames would stay alive.
            exc.__traceback__ = None
            with self._lock:
                if generation != self._generation:
                    logger.debug(
                        "Discarding death of stale reader session of %r (gen %d, current %d): %r",
                        self.name,
                        generation,
                        self._generation,
                        exc,
                    )
                    return
                self._pending_exc = exc
                self._last_session_error = exc
                if self._gen_emits == 0:
                    self._dead_sessions += 1
            logger.warning("Reader of %r died: %r -- the next poll() re-raises it", self.name, exc)
        else:
            with self._lock:
                if generation != self._generation:
                    return
                if self._gen_emits == 0:
                    # A clean return that delivered nothing counts toward the
                    # dead-session escalation too: from the pipeline's point of
                    # view it is indistinguishable from a dead device.
                    self._dead_sessions += 1
