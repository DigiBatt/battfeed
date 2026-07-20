"""StreamingSource tests -- scripted fake readers only: no hardware, no long sleeps."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

import pytest

from battfeed import DeadReaderError, Harvester, SourceFailure, StreamingSource
from battfeed.protocols import Sample

#: The only real sleep used anywhere here (idle loops and wait_for polling).
TICK_S = 0.001
Session = Callable[[Callable[[Sample], None], Callable[[], bool]], None]


def wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Poll a condition at 1 ms until true; fail the test on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(TICK_S)
    raise AssertionError("condition not met within timeout")


def idle_until_stop(emit: Callable[[Sample], None], should_stop: Callable[[], bool]) -> None:
    while not should_stop():
        time.sleep(TICK_S)


def emit_then_idle(
    samples: list[Sample],
    *,
    gate: threading.Event | None = None,
    done: threading.Event | None = None,
) -> Session:
    """A session that (optionally after ``gate``) emits ``samples``, flags ``done``, idles."""

    def session(emit: Callable[[Sample], None], should_stop: Callable[[], bool]) -> None:
        if gate is not None:
            assert gate.wait(5.0)
        for sample in samples:
            emit(sample)
        if done is not None:
            done.set()
        idle_until_stop(emit, should_stop)

    return session


class ScriptedSource(StreamingSource):
    """Each reader start consumes the next scripted session; then it idles."""

    def __init__(self, sessions: list[Session], **kwargs: Any) -> None:
        super().__init__("scripted", **kwargs)
        self._sessions = list(sessions)

    def run_reader(self, emit: Callable[[Sample], None], should_stop: Callable[[], bool]) -> None:
        if self._sessions:
            self._sessions.pop(0)(emit, should_stop)
        else:
            idle_until_stop(emit, should_stop)


def drain(
    source: StreamingSource,
    count: int,
    timeout: float = 5.0,
    *,
    into: list[Sample] | None = None,
) -> list[Sample]:
    """Poll until ``count`` samples have been collected in total (order preserved).

    ``into`` carries samples already collected by earlier polls -- the reader
    thread may emit before the very first drain, so tests must never discard
    a poll() result.
    """
    collected: list[Sample] = into if into is not None else []
    deadline = time.monotonic() + timeout
    while len(collected) < count and time.monotonic() < deadline:
        collected.extend(source.poll())
    assert len(collected) == count, f"drained {len(collected)} of {count} samples"
    return collected


def test_base_run_reader_is_abstract() -> None:
    source = StreamingSource("bare")
    source.poll()  # starts the reader thread, which dies on NotImplementedError
    wait_for(lambda: not source.reader_alive)
    with pytest.raises(NotImplementedError, match="run_reader"):
        source.poll()
    source.close()


def test_buffer_size_must_be_positive() -> None:
    with pytest.raises(ValueError, match="buffer_size"):
        ScriptedSource([], buffer_size=0)


def test_samples_arrive_in_emit_order_across_polls() -> None:
    gate, done = threading.Event(), threading.Event()
    samples: list[Sample] = [{"i": i, "voltage_volt": 3.0} for i in range(20)]
    source = ScriptedSource([emit_then_idle(samples, gate=gate, done=done)])
    try:
        # First poll starts the reader lazily; the gate keeps it from emitting
        # yet, so the empty first drain is deterministic.
        assert source.poll() == []
        assert source.reader_alive
        gate.set()
        assert done.wait(5.0)
        assert [row["i"] for row in drain(source, 20)] == list(range(20))
        assert source.received_total == 20
        assert source.dropped_total == 0
    finally:
        source.close()


def test_overflow_drops_oldest_and_counts(caplog: pytest.LogCaptureFixture) -> None:
    gate, done = threading.Event(), threading.Event()
    samples: list[Sample] = [{"i": i} for i in range(5)]
    source = ScriptedSource([emit_then_idle(samples, gate=gate, done=done)], buffer_size=3)
    try:
        source.poll()  # start the reader; it blocks on the gate
        with caplog.at_level(logging.WARNING, logger="battfeed.sources.streaming"):
            gate.set()  # now all 5 samples are emitted with nothing draining
            assert done.wait(5.0)
            batch = source.poll()
        # The 3 newest survive; the 2 oldest were dropped -- and counted (I2).
        assert [row["i"] for row in batch] == [2, 3, 4]
        assert source.dropped_total == 2
        assert source.received_total == 5
        assert any("overflowed" in record.message for record in caplog.records)
        assert source.stream_stats()["dropped_total"] == 2
    finally:
        source.close()


def test_overflow_warning_is_rate_limited(fake_clock) -> None:
    source = ScriptedSource([], buffer_size=1, clock=fake_clock)
    warned: list[int] = []

    # Count warn decisions via the counter reset: _dropped_since_warn returns
    # to 0 exactly when a warning is issued. No session is started, so the
    # current generation accepts these direct emits.
    def emit_and_note(sample: Sample) -> None:
        source._emit(source._generation, sample)
        if source._dropped_since_warn == 0 and source.dropped_total:
            warned.append(source.dropped_total)

    for i in range(4):  # 3 overflows at fake-time 0: only the first may warn
        emit_and_note({"i": i})
    assert source.dropped_total == 3
    assert warned == [1]
    fake_clock.sleep(11.0)  # past the warn interval: the next overflow warns again
    emit_and_note({"i": 99})
    assert warned == [1, 4]
    source.close()


def test_reader_error_raises_on_next_poll_then_restarts() -> None:
    boom = RuntimeError("adapter unplugged")

    def dying_session(emit: Callable[[Sample], None], should_stop: Callable[[], bool]) -> None:
        emit({"i": 1})
        raise boom

    done = threading.Event()
    source = ScriptedSource([dying_session, emit_then_idle([{"i": 99}], done=done)])
    try:
        rows = source.poll()  # starts the reader, which emits once and dies
        wait_for(lambda: not source.reader_alive)
        with pytest.raises(RuntimeError) as excinfo:
            source.poll()  # the death surfaces here (invariant I4)...
        assert excinfo.value is boom  # ...as the exact reader exception
        # The poll AFTER the raising one restarts the reader (second session).
        drain(source, 2, into=rows)
        assert done.wait(5.0)
        # The pre-crash sample was never lost (invariant I2).
        assert [row["i"] for row in rows] == [1, 99]
        assert source.reader_restarts == 1
    finally:
        source.close()


def test_reader_that_returns_cleanly_is_restarted() -> None:
    def short_session(emit: Callable[[Sample], None], should_stop: Callable[[], bool]) -> None:
        emit({"i": 1})  # returns without waiting for stop (e.g. a scan ended)

    done = threading.Event()
    source = ScriptedSource([short_session, emit_then_idle([{"i": 2}], done=done)])
    try:
        rows = source.poll()
        wait_for(lambda: not source.reader_alive)
        drain(source, 2, into=rows)  # no exception: the restart is silent
        assert [row["i"] for row in rows] == [1, 2]
        assert source.reader_restarts == 1
    finally:
        source.close()


def test_close_is_idempotent_and_poll_reconnects() -> None:
    first_done, second_done = threading.Event(), threading.Event()
    source = ScriptedSource(
        [
            emit_then_idle([{"i": 1}], done=first_done),
            emit_then_idle([{"i": 2}], done=second_done),
        ]
    )
    rows = source.poll()
    assert first_done.wait(5.0)
    assert [row["i"] for row in drain(source, 1, into=rows)] == [1]
    source.close()
    assert not source.reader_alive
    source.close()  # idempotent

    rows = drain(source, 1)  # poll() after close() reconnects (second session)
    assert second_done.wait(5.0)
    assert [row["i"] for row in rows] == [2]
    assert source.reader_alive
    source.close()
    assert not source.reader_alive


def test_close_discards_pending_error_so_reconnect_is_clean() -> None:
    def dying_session(emit: Callable[[Sample], None], should_stop: Callable[[], bool]) -> None:
        raise RuntimeError("boom")

    done = threading.Event()
    source = ScriptedSource([dying_session, emit_then_idle([{"i": 7}], done=done)])
    source.poll()  # the dying session emits nothing, so nothing is discarded
    wait_for(lambda: not source.reader_alive)
    source.close()  # swallows (and logs) the pending error
    assert [row["i"] for row in drain(source, 1)] == [7]  # clean restart, no stale raise
    source.close()


def test_concurrent_emit_while_draining_loses_nothing() -> None:
    n = 2000
    done = threading.Event()

    def firehose(emit: Callable[[Sample], None], should_stop: Callable[[], bool]) -> None:
        for i in range(n):
            emit({"i": i})
        done.set()
        idle_until_stop(emit, should_stop)

    source = ScriptedSource([firehose], buffer_size=n + 10)
    try:
        # The first poll starts the reader; from then on emit races the drain.
        rows = drain(source, n, timeout=10.0, into=source.poll())
        assert [row["i"] for row in rows] == list(range(n))  # ordered, none lost or duplicated
        assert source.received_total == n
        assert source.dropped_total == 0
    finally:
        source.close()


def test_metadata_carries_stream_stats_and_subclasses_merge() -> None:
    class ShuntLike(ScriptedSource):
        def metadata(self):
            return {**super().metadata(), "instrument_model": "Fake Shunt 500A"}

    done = threading.Event()
    source = ShuntLike([emit_then_idle([{"i": 1}], done=done)])
    try:
        rows = source.poll()
        assert done.wait(5.0)
        drain(source, 1, into=rows)
        metadata = source.metadata()
        assert metadata["source"] == "scripted"
        assert metadata["kind"] == "streaming"
        assert metadata["instrument_model"] == "Fake Shunt 500A"
        assert metadata["received_total"] == 1
        assert metadata["dropped_total"] == 0
        assert metadata["reader_restarts"] == 0
        assert metadata["buffer_size"] == 4096
    finally:
        source.close()


# -- dead-session escalation (invariant I4, liveness) ------------------------
def dying(emit: Callable[[Sample], None], should_stop: Callable[[], bool]) -> None:
    raise ConnectionError("device gone")


def test_restart_poll_raises_dead_reader_error_after_sample_less_death() -> None:
    source = ScriptedSource([dying, dying])
    try:
        source.poll()  # first-ever start: never raises
        wait_for(lambda: not source.reader_alive)
        with pytest.raises(ConnectionError):  # parked death of session 1
            source.poll()
        with pytest.raises(DeadReaderError) as first:  # honest restart, not a fake []
            source.poll()
        assert first.value.dead_sessions == 1
        assert isinstance(first.value.last_error, ConnectionError)
        wait_for(lambda: not source.reader_alive)  # session 2 died too
        with pytest.raises(ConnectionError):
            source.poll()
        with pytest.raises(DeadReaderError) as second:
            source.poll()
        assert second.value.dead_sessions == 2  # the escalation genuinely accumulates
        assert second.value is not first.value  # a fresh exception per restart
    finally:
        source.close()


def test_permanently_dead_reader_trips_source_failure_under_default_policy(
    fake_clock, list_sink
) -> None:
    """The atk1 scenario: a dead device must reach SourceFailure, DEFAULT policy.

    Every poll during the dead period raises (parked exception or
    DeadReaderError on the restarting poll), so the harvester's consecutive
    counter accumulates and its backoff escalates instead of being reset by
    fabricated [] successes and pinned at the minimum.
    """

    class AlwaysDead(StreamingSource):
        def __init__(self) -> None:
            super().__init__("dead-device")

        def run_reader(
            self, emit: Callable[[Sample], None], should_stop: Callable[[], bool]
        ) -> None:
            raise ConnectionError("device permanently gone")

    source = AlwaysDead()
    harvester = Harvester()
    harvester.register(source)

    def sleep(seconds: float) -> None:
        # Advance fake time, then wait (bounded, real) for the current reader
        # session to be dead -- making every next poll's view deterministic.
        fake_clock.sleep(seconds)
        wait_for(lambda: not source.reader_alive)

    try:
        with pytest.raises(SourceFailure) as excinfo:
            harvester.collect(  # errors defaults to ErrorPolicy(): max 5 consecutive
                "dead-device",
                duration_s=100_000.0,
                interval_s=1.0,
                sink=list_sink,
                clock=fake_clock,
                sleep=sleep,
            )
        assert isinstance(excinfo.value.last_error, ConnectionError)
        # First sleep is the interval after the silent first start; then the
        # ESCALATING backoff series -- not pinned at backoff(1) forever.
        assert fake_clock.sleeps == [1.0, 1.0, 2.0, 4.0, 8.0]
        assert list_sink.rows == []
    finally:
        source.close()


def test_flaky_then_recovering_reader_does_not_false_trip(fake_clock, list_sink) -> None:
    """One sample-less crash, then a healthy session: collect completes normally."""
    healthy_started = threading.Event()

    def healthy(emit: Callable[[Sample], None], should_stop: Callable[[], bool]) -> None:
        emit({"voltage_volt": 3.7})
        healthy_started.set()  # after the emit: the next poll definitely sees the sample
        idle_until_stop(emit, should_stop)

    source = ScriptedSource([dying, healthy])
    harvester = Harvester()
    harvester.register(source)

    def sleep(seconds: float) -> None:
        fake_clock.sleep(seconds)
        deadline = time.monotonic() + 5.0
        while source.reader_alive and not healthy_started.is_set() and time.monotonic() < deadline:
            time.sleep(TICK_S)

    try:
        stats = harvester.collect(  # DEFAULT policy again
            "scripted",
            duration_s=10.0,
            interval_s=1.0,
            sink=list_sink,
            clock=fake_clock,
            sleep=sleep,
        )
        # The crash and the honest restart were tolerated, then forgotten.
        assert stats.errors == 2
        assert any(row.get("voltage_volt") == 3.7 for row in list_sink.rows)
    finally:
        source.close()


# -- generation guard (zombie sessions cannot contaminate) --------------------
def test_zombie_reader_cannot_contaminate_after_close() -> None:
    release = threading.Event()

    def zombie(emit: Callable[[Sample], None], should_stop: Callable[[], bool]) -> None:
        # A realistic bug: blocked in a long wait that ignores should_stop,
        # then waking up to emit and die long after being abandoned.
        release.wait(10.0)
        emit({"z": 1})
        raise RuntimeError("zombie death")

    done = threading.Event()
    source = ScriptedSource([zombie, emit_then_idle([{"i": 2}], done=done)], join_timeout_s=0.01)
    started = time.monotonic()
    source.poll()  # starts the zombie session
    zombie_thread = source._thread
    assert zombie_thread is not None
    source.close()  # join times out at 0.01 s: close returns promptly anyway
    assert time.monotonic() - started < 2.0
    assert not source.reader_alive  # best-effort: the abandoned zombie no longer counts

    rows = source.poll()  # reconnect: a NEW session (new generation)
    assert done.wait(5.0)
    release.set()  # now the zombie wakes, emits and dies
    zombie_thread.join(5.0)
    assert not zombie_thread.is_alive()

    drain(source, 1, into=rows)
    assert [row["i"] for row in rows] == [2]  # the zombie's sample never appeared
    assert all("z" not in row for row in rows)
    assert source.poll() == []  # and its death exception never surfaces
    assert source.received_total == 1  # only the live session's emit was accepted
    source.close()


# -- concurrency of the restart path itself -----------------------------------
def test_concurrent_polls_survive_constant_restarts() -> None:
    """Hammer the restart bookkeeping from many threads: expected raises only."""

    class InstantDeath(StreamingSource):
        def __init__(self) -> None:
            super().__init__("instant-death")

        def run_reader(
            self, emit: Callable[[Sample], None], should_stop: Callable[[], bool]
        ) -> None:
            raise ConnectionError("gone")

    source = InstantDeath()
    unexpected: list[BaseException] = []

    def worker() -> None:
        for _ in range(150):
            try:
                source.poll()
            except (ConnectionError, DeadReaderError):
                pass  # the two honest failure modes
            except BaseException as exc:  # anything else is a bookkeeping crash
                unexpected.append(exc)

    workers = [threading.Thread(target=worker) for _ in range(8)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join(30.0)
    assert unexpected == []
    source.close()


# -- parked-exception traceback hygiene ---------------------------------------
def test_parked_exceptions_do_not_accumulate_traceback() -> None:
    """A reader re-raising one cached instance must not grow its traceback."""
    cached = ConnectionError("cached instance")

    def raise_cached(emit: Callable[[Sample], None], should_stop: Callable[[], bool]) -> None:
        raise cached

    def tb_depth(exc: BaseException) -> int:
        depth, tb = 0, exc.__traceback__
        while tb is not None:
            depth += 1
            tb = tb.tb_next
        return depth

    source = ScriptedSource([raise_cached, raise_cached])
    try:
        source.poll()
        wait_for(lambda: not source.reader_alive)
        with pytest.raises(ConnectionError):
            source.poll()
        first_depth = tb_depth(cached)
        with pytest.raises(DeadReaderError):
            source.poll()  # restart -> session 2 raises the SAME instance again
        wait_for(lambda: not source.reader_alive)
        with pytest.raises(ConnectionError):
            source.poll()
        # Parking cleared the old traceback, so re-raising starts fresh
        # instead of appending: constant depth, not linear growth.
        assert tb_depth(cached) == first_depth
    finally:
        source.close()
