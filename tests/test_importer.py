"""run_import driver and the ``battfeed import`` CLI verb."""

from __future__ import annotations

import csv
import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from battfeed import ErrorPolicy, ImportLedger, ImportStats, SourceFailure, cli, run_import


class RowsSink:
    """Minimal in-memory sink for tests needing more than the list_sink fixture."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def write(self, rows) -> None:
        self.rows.extend(dict(row) for row in rows)

    def close(self) -> None:
        pass


def row(t: float, series: str | None = None, run: str | None = None) -> dict:
    sample: dict = {"test_time_second": t, "voltage_volt": 3.7, "current_ampere": -0.5}
    if series is not None:
        sample["series_id"] = series
    if run is not None:
        sample["run_id"] = run
    return sample


class BatchSource:
    """Batch source WITHOUT the optional drained() hook."""

    name = "fakebatch"

    def __init__(self, polls: list[list[dict]]) -> None:
        self._polls = list(polls)
        self.poll_count = 0

    def metadata(self) -> dict:
        return {"source": self.name}

    def poll(self) -> list[dict]:
        self.poll_count += 1
        return self._polls.pop(0) if self._polls else []


class PacedSource(BatchSource):
    """Batch source WITH drained(): not drained until its scripted polls run out."""

    def drained(self) -> bool:
        return not self._polls


class FlakySource(BatchSource):
    """Raises for the first ``failures`` polls, then delegates to the script."""

    def __init__(self, polls: list[list[dict]], failures: int) -> None:
        super().__init__(polls)
        self._failures = failures

    def poll(self) -> list[dict]:
        if self._failures > 0:
            self._failures -= 1
            raise ConnectionError("device unplugged")
        return super().poll()


# -- one-shot mode -----------------------------------------------------------


def test_one_shot_drains_source_without_drained_hook(fake_clock, list_sink):
    source = BatchSource([[row(0.0), row(1.0)], [row(0.0)]])
    stats = run_import(source, list_sink, interval_s=5.0, clock=fake_clock, sleep=fake_clock.sleep)
    assert [r["test_time_second"] for r in list_sink.rows] == [0.0, 1.0, 0.0]
    assert stats.samples == 3
    assert stats.batches == 2
    assert stats.polls == 3  # two productive polls + the empty one that drained it
    assert stats.errors == 0
    assert source.poll_count == 3  # stopped at the FIRST empty poll (no hook)
    assert fake_clock.sleeps == []  # backlog drains without idling


def test_one_shot_respects_drained_hook_returning_false(fake_clock, list_sink):
    # An empty poll mid-script: without the hook the driver would stop here;
    # with it, the driver idles one interval and keeps going.
    source = PacedSource([[row(0.0)], [], [row(0.0)]])
    stats = run_import(
        source,
        list_sink,
        interval_s=5.0,
        stop=threading.Event(),  # drained() may defer, so a stop event is required
        clock=fake_clock,
        sleep=fake_clock.sleep,
    )
    assert stats.samples == 2
    assert stats.polls == 4  # batch, empty (not drained), batch, empty (drained)
    assert fake_clock.sleeps == [5.0]  # exactly the one not-yet-drained idle


def test_one_shot_with_drained_hook_requires_stop_event(fake_clock, list_sink):
    # A drained() hook can keep a one-shot run polling forever, so -- exactly
    # like watch mode -- it needs a stop event to be stoppable.
    with pytest.raises(ValueError, match="drained"):
        run_import(PacedSource([]), list_sink, clock=fake_clock, sleep=fake_clock.sleep)


def test_one_shot_stats_columns_and_source_name(fake_clock, list_sink):
    source = BatchSource([[row(0.0, series="packA", run="f1")]])
    stats = run_import(source, list_sink, clock=fake_clock, sleep=fake_clock.sleep)
    assert stats.source == "fakebatch"
    assert stats.columns == sorted(
        ["test_time_second", "voltage_volt", "current_ampere", "series_id", "run_id"]
    )


def test_driver_never_stamps_test_time_second(fake_clock, list_sink):
    """Invariant I5: the source owns the timebase; the driver passes rows through."""
    source = BatchSource([[{"voltage_volt": 3.7, "current_ampere": 0.0}]])
    run_import(source, list_sink, clock=fake_clock, sleep=fake_clock.sleep)
    assert "test_time_second" not in list_sink.rows[0]


def test_stop_event_pre_set_means_no_polls(fake_clock, list_sink):
    source = BatchSource([[row(0.0)]])
    stop = threading.Event()
    stop.set()
    stats = run_import(source, list_sink, stop=stop, clock=fake_clock, sleep=fake_clock.sleep)
    assert stats.samples == 0
    assert source.poll_count == 0


# -- watch mode --------------------------------------------------------------


def test_watch_mode_polls_until_stop_event(fake_clock, list_sink):
    stop = threading.Event()

    class StoppingSource(BatchSource):
        def poll(self) -> list[dict]:
            batch = super().poll()
            if self.poll_count >= 5:
                stop.set()  # stand-in for Ctrl-C arriving later
            return batch

    source = StoppingSource([[row(0.0)], [], [], [row(0.0)]])
    stats = run_import(
        source,
        list_sink,
        watch=True,
        interval_s=2.0,
        stop=stop,
        clock=fake_clock,
        sleep=fake_clock.sleep,
    )
    assert stats.samples == 2
    assert source.poll_count == 5
    # Productive polls re-poll immediately; only the empty ones idle. The
    # final (empty, stop-setting) poll must NOT be followed by a sleep.
    assert fake_clock.sleeps == [2.0, 2.0]


def test_watch_mode_without_stop_event_is_rejected(fake_clock, list_sink):
    with pytest.raises(ValueError, match="stop event"):
        run_import(
            BatchSource([]),
            list_sink,
            watch=True,
            clock=fake_clock,
            sleep=fake_clock.sleep,
        )


def test_non_positive_interval_is_rejected(fake_clock, list_sink):
    with pytest.raises(ValueError, match="interval_s"):
        run_import(BatchSource([]), list_sink, interval_s=0, clock=fake_clock)


# -- error policy reuse ------------------------------------------------------


def test_transient_failures_are_retried_with_backoff(fake_clock, list_sink):
    source = FlakySource([[row(0.0)]], failures=2)
    stats = run_import(
        source,
        list_sink,
        errors=ErrorPolicy(backoff_initial_s=1.0, backoff_factor=2.0),
        clock=fake_clock,
        sleep=fake_clock.sleep,
    )
    assert stats.samples == 1
    assert stats.errors == 2
    assert fake_clock.sleeps == [1.0, 2.0]  # exponential backoff, then success


def test_source_failure_after_error_allowance(fake_clock, list_sink):
    source = FlakySource([], failures=10)
    with pytest.raises(SourceFailure) as excinfo:
        run_import(
            source,
            list_sink,
            errors=ErrorPolicy(max_consecutive_errors=3),
            clock=fake_clock,
            sleep=fake_clock.sleep,
        )
    assert excinfo.value.consecutive == 3
    assert list_sink.rows == []


def test_errors_none_fails_fast(fake_clock, list_sink):
    with pytest.raises(ConnectionError):
        run_import(
            FlakySource([], failures=1),
            list_sink,
            errors=None,
            clock=fake_clock,
            sleep=fake_clock.sleep,
        )


# -- commit point (at-least-once) --------------------------------------------


class LedgeredFolderSource:
    """Follows the ImportLedger docstring pattern: commit AFTER the write."""

    name = "folder"

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.ledger = ImportLedger(directory / ".ledger.json")
        self._pending: tuple[Path, str] | None = None

    def metadata(self) -> dict:
        return {"source": self.name}

    def poll(self) -> list[dict]:
        for path in sorted(self.directory.glob("*.txt")):
            digest = self.ledger.hash_of(path)
            if digest is None:
                continue
            if self.ledger.seen(path, content_hash=digest):
                continue
            if self.ledger.is_quarantined(path, content_hash=digest):
                continue
            self._pending = (path, digest)
            return [row(float(i), series=path.stem, run="r1") for i in range(3)]
        return []

    def drained(self) -> bool:
        return True

    def commit_batch(self) -> None:
        if self._pending is not None:
            path, digest = self._pending
            self.ledger.record(path, content_hash=digest)
            self._pending = None


def test_commit_batch_called_after_each_successful_write(fake_clock):
    events: list[str] = []

    class CommittingSource(BatchSource):
        def commit_batch(self) -> None:
            events.append("commit")

    class OrderSink:
        def write(self, rows) -> None:
            events.append("write")

        def close(self) -> None:
            pass

    run_import(
        CommittingSource([[row(0.0)], [row(1.0)]]),
        OrderSink(),
        clock=fake_clock,
        sleep=fake_clock.sleep,
    )
    assert events == ["write", "commit", "write", "commit"]


def test_sink_failure_leaves_file_uncommitted_then_reimported(tmp_path, fake_clock):
    """C1: a sink failure between poll and write must NOT mark the file ingested."""
    data = tmp_path / "data"
    data.mkdir()
    flight = data / "flight1.txt"
    flight.write_text("the only copy of flight 1", encoding="utf-8")

    class FailingSink:
        def write(self, rows) -> None:
            raise OSError("disk full")

        def close(self) -> None:
            pass

    source = LedgeredFolderSource(data)
    with pytest.raises(OSError):
        run_import(
            source,
            FailingSink(),
            stop=threading.Event(),
            clock=fake_clock,
            sleep=fake_clock.sleep,
        )
    assert not source.ledger.seen(flight)  # nothing committed -> not lost

    retry = LedgeredFolderSource(data)  # a fresh run re-imports the file
    sink = RowsSink()
    stats = run_import(
        retry, sink, stop=threading.Event(), clock=fake_clock, sleep=fake_clock.sleep
    )
    assert stats.samples == 3
    assert retry.ledger.seen(flight)

    third = LedgeredFolderSource(data)  # and only then is it deduplicated
    stats = run_import(
        third, RowsSink(), stop=threading.Event(), clock=fake_clock, sleep=fake_clock.sleep
    )
    assert stats.samples == 0


_CRASH_WORKER = """
import os
import sys
import threading
from pathlib import Path

from battfeed import ImportLedger, RoutingSink, run_import

data = Path(sys.argv[1])
out = Path(sys.argv[2])
crash = sys.argv[3] == "crash"


class FolderSource:
    name = "folder"

    def __init__(self):
        self.ledger = ImportLedger(data / ".ledger.json")
        self._pending = None

    def metadata(self):
        return {"source": self.name}

    def poll(self):
        for path in sorted(data.glob("*.txt")):
            digest = self.ledger.hash_of(path)
            if digest is None or self.ledger.seen(path, content_hash=digest):
                continue
            self._pending = (path, digest)
            return [
                {"test_time_second": float(i), "voltage_volt": 3.7,
                 "current_ampere": -1.0, "series_id": path.stem, "run_id": "r1"}
                for i in range(3)
            ]
        return []

    def drained(self):
        return True

    def commit_batch(self):
        if self._pending is not None:
            path, digest = self._pending
            self.ledger.record(path, content_hash=digest)
            self._pending = None


class CrashingSink:
    def __init__(self, inner):
        self.inner = inner

    def write(self, rows):
        if crash:
            os._exit(1)  # power loss between poll() and the write completing
        self.inner.write(rows)

    def close(self):
        self.inner.close()


sink = CrashingSink(RoutingSink(out, institution="TEST"))
stats = run_import(FolderSource(), sink, stop=threading.Event())
sink.close()
print(f"imported {stats.samples}")
"""


def test_crash_between_poll_and_write_reimports_on_restart(tmp_path):
    """C1: kill -9 mid-import must lose nothing -- the file re-imports next run."""
    data = tmp_path / "data"
    data.mkdir()
    out = tmp_path / "out"
    (data / "flight1.txt").write_text("the only copy of flight 1", encoding="utf-8")
    worker = tmp_path / "worker.py"
    worker.write_text(_CRASH_WORKER, encoding="utf-8")

    crashed = subprocess.run(
        [sys.executable, str(worker), str(data), str(out), "crash"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert crashed.returncode == 1  # died inside the sink's first write
    ledger_file = data / ".ledger.json"
    if ledger_file.exists():  # may not exist: no mutation happened before the crash
        assert json.loads(ledger_file.read_text(encoding="utf-8"))["ingested"] == {}

    restarted = subprocess.run(
        [sys.executable, str(worker), str(data), str(out), "nocrash"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert restarted.returncode == 0, restarted.stderr
    assert "imported 3" in restarted.stdout
    files = list(out.glob("*.bdf.csv"))
    assert len(files) == 1
    data_rows = files[0].read_text(encoding="utf-8").splitlines()[1:]
    assert len(data_rows) == 3  # the flight's rows made it to disk after all


# -- invariant I5 warning ----------------------------------------------------


def test_warns_once_per_run_for_rows_missing_test_time_second(fake_clock, list_sink, caplog):
    batches = [
        [{"voltage_volt": 3.7, "current_ampere": 0.0}],
        [{"voltage_volt": 3.6, "current_ampere": 0.0}],  # second offender: no second warning
    ]
    with caplog.at_level(logging.WARNING, logger="battfeed.importer"):
        run_import(BatchSource(batches), list_sink, clock=fake_clock, sleep=fake_clock.sleep)
    warnings = [r for r in caplog.records if "test_time_second" in r.getMessage()]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "'fakebatch'" in message
    assert "I5" in message


def test_no_warning_when_rows_carry_test_time_second(fake_clock, list_sink, caplog):
    with caplog.at_level(logging.WARNING, logger="battfeed.importer"):
        run_import(
            BatchSource([[row(0.0)], [row(1.0)]]),
            list_sink,
            clock=fake_clock,
            sleep=fake_clock.sleep,
        )
    assert not [r for r in caplog.records if "test_time_second" in r.getMessage()]


# -- stop responsiveness -----------------------------------------------------


def test_stop_during_long_backoff_interrupts_promptly(list_sink):
    """With the default sleep, waits are stop.wait -- a stop request need not
    ride out a 30 s backoff. (Deliberately real-time: bounded well under 1 s.)"""
    source = FlakySource([], failures=1_000_000)
    stop = threading.Event()
    policy = ErrorPolicy(max_consecutive_errors=99, backoff_initial_s=30.0, backoff_max_s=30.0)
    threading.Timer(0.05, stop.set).start()
    started = time.perf_counter()
    stats = run_import(source, list_sink, watch=True, stop=stop, errors=policy)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0  # nowhere near the 30 s backoff
    assert stats.errors >= 1


# -- CLI: battfeed import ----------------------------------------------------


class FakeFlightSource:
    """In-process import source: one 'flight' per poll, routed per pack."""

    name = "fakeflights"

    def __init__(self, flights: int = 2) -> None:
        self.reset_calls = 0
        self.closed = False
        self._pending = [
            [row(float(t), series=f"pack-{n}", run=f"flight-{n}") for t in range(3)]
            for n in range(flights)
        ]

    def metadata(self) -> dict:
        return {"source": self.name, "kind": "fake-import"}

    def poll(self) -> list[dict]:
        return self._pending.pop(0) if self._pending else []

    def drained(self) -> bool:
        return not self._pending

    def reset_ledger(self) -> None:
        self.reset_calls += 1

    def close(self) -> None:
        self.closed = True


class NoResetSource(FakeFlightSource):
    reset_ledger = None  # type: ignore[assignment]


@pytest.fixture
def fake_flights(monkeypatch):
    """Register FakeFlightSource in-process under the name 'fakeflights'."""
    created: list[FakeFlightSource] = []

    def factory(name: str, **kwargs):
        if name != "fakeflights":
            raise KeyError(f"Unknown source {name!r}. Available sources: ['fakeflights']")
        source = FakeFlightSource(**kwargs)
        created.append(source)
        return source

    monkeypatch.setattr(cli, "create_source", factory)
    return created


def test_cli_import_smoke_one_file_per_series_run(tmp_path, capsys, fake_flights):
    exit_code = cli.main(
        [
            "import",
            "--source",
            "fakeflights",
            "--out-dir",
            str(tmp_path),
            "--institution",
            "TEST",
        ]
    )
    assert exit_code == 0

    files = sorted(tmp_path.glob("*.bdf.csv"))
    assert len(files) == 2  # two packs, one flight each
    for path in files:
        with open(path, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == 3
        header = rows[0].keys()
        assert "series_id" not in header and "run_id" not in header
        assert [r["test_time_second"] for r in rows] == ["0.0", "1.0", "2.0"]

        sidecar = json.loads(
            path.with_name(path.name.removesuffix(".bdf.csv") + ".meta.json").read_text(
                encoding="utf-8"
            )
        )
        assert sidecar["finalized"] is True
        assert sidecar["rows"] == 3
        assert sidecar["metadata"]["series_id"] in ("pack-0", "pack-1")
        assert sidecar["metadata"]["run_id"] in ("flight-0", "flight-1")
        assert sidecar["metadata"]["institution"] == "TEST"
        assert sidecar["metadata"]["source"]["kind"] == "fake-import"

    out = capsys.readouterr().out
    assert "Imported 6 sample(s)" in out
    assert "2 file(s)" in out
    for path in files:
        assert path.name in out

    (source,) = fake_flights
    assert source.closed  # the CLI closes the source when done


def test_cli_import_passes_opts_to_the_source(tmp_path, capsys, fake_flights):
    assert (
        cli.main(
            ["import", "--source", "fakeflights", "--opt", "flights=1", "--out-dir", str(tmp_path)]
        )
        == 0
    )
    assert len(list(tmp_path.glob("*.bdf.csv"))) == 1


def test_cli_import_nothing_to_import(tmp_path, capsys, fake_flights):
    exit_code = cli.main(
        ["import", "--source", "fakeflights", "--opt", "flights=0", "--out-dir", str(tmp_path)]
    )
    assert exit_code == 0
    assert "Nothing to import from 'fakeflights'" in capsys.readouterr().out
    assert list(tmp_path.glob("*")) == []  # no files, not even empty ones


def test_cli_import_reset_ledger_calls_the_hook(tmp_path, capsys, fake_flights):
    exit_code = cli.main(
        [
            "import",
            "--source",
            "fakeflights",
            "--opt",
            "flights=0",
            "--out-dir",
            str(tmp_path),
            "--reset-ledger",
        ]
    )
    assert exit_code == 0
    (source,) = fake_flights
    assert source.reset_calls == 1
    assert "Reset the import ledger" in capsys.readouterr().err


def test_cli_import_reset_ledger_without_hook_fails_cleanly(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "create_source", lambda name, **kwargs: NoResetSource(**kwargs))
    exit_code = cli.main(
        ["import", "--source", "fakeflights", "--out-dir", str(tmp_path), "--reset-ledger"]
    )
    assert exit_code == 2
    assert "does not support --reset-ledger" in capsys.readouterr().err
    assert list(tmp_path.glob("*")) == []


def test_cli_import_unknown_source_fails_cleanly(tmp_path, capsys, fake_flights):
    assert cli.main(["import", "--source", "nope", "--out-dir", str(tmp_path)]) == 2
    assert "Unknown source" in capsys.readouterr().err


def test_cli_import_forwards_watch_and_interval(tmp_path, monkeypatch, fake_flights, capsys):
    recorded: dict = {}

    def fake_run_import(source, sink, **kwargs):
        recorded.update(kwargs)
        return ImportStats(
            samples=0, polls=1, batches=0, duration_s=0.0, started_at="", source=source.name
        )

    monkeypatch.setattr(cli, "run_import", fake_run_import)
    exit_code = cli.main(
        [
            "import",
            "--source",
            "fakeflights",
            "--out-dir",
            str(tmp_path),
            "--watch",
            "--interval",
            "0.5",
        ]
    )
    assert exit_code == 0
    assert recorded["watch"] is True
    assert recorded["interval_s"] == 0.5
    assert recorded["stop"] is not None
    assert "Watching 'fakeflights'" in capsys.readouterr().err


def test_cli_import_source_failure_exits_1(tmp_path, capsys, monkeypatch):
    class DoomedSource:
        name = "doomed"

        def metadata(self) -> dict:
            return {"source": self.name}

        def poll(self) -> list[dict]:
            raise ConnectionError("gone")

    monkeypatch.setattr(cli, "create_source", lambda name, **kwargs: DoomedSource())

    def failing_run_import(source, sink, **kwargs):
        raise SourceFailure(source.name, 5, ConnectionError("gone"))

    monkeypatch.setattr(cli, "run_import", failing_run_import)
    exit_code = cli.main(["import", "--source", "doomed", "--out-dir", str(tmp_path)])
    assert exit_code == 1
    assert "failed 5 times" in capsys.readouterr().err


def test_cli_import_torn_ledger_exits_2_with_clean_message(tmp_path, capsys, monkeypatch):
    """A corrupt ledger raising ValueError in the source constructor must reach
    the operator as one actionable line, not a traceback."""

    def torn(name, **kwargs):
        raise ValueError(
            "Import ledger .ledger.json is not valid JSON (Expecting value). "
            "Refusing to treat it as empty; delete the file to start fresh."
        )

    monkeypatch.setattr(cli, "create_source", torn)
    exit_code = cli.main(["import", "--source", "ledgered", "--out-dir", str(tmp_path)])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "not valid JSON" in err
    assert "Traceback" not in err


def test_cli_import_bad_institution_exits_2_and_closes_source(tmp_path, capsys, fake_flights):
    exit_code = cli.main(
        [
            "import",
            "--source",
            "fakeflights",
            "--out-dir",
            str(tmp_path),
            "--institution",
            "BAD__CODE",
        ]
    )
    assert exit_code == 2
    assert "must not contain '__'" in capsys.readouterr().err
    (source,) = fake_flights
    assert source.closed  # usage errors must still release the source
    assert list(tmp_path.glob("*")) == []


def test_cli_import_nonpositive_interval_exits_2_and_closes_source(tmp_path, capsys, fake_flights):
    exit_code = cli.main(
        ["import", "--source", "fakeflights", "--out-dir", str(tmp_path), "--interval", "0"]
    )
    assert exit_code == 2
    assert "--interval must be positive" in capsys.readouterr().err
    (source,) = fake_flights
    assert source.closed
