"""Error policy, backoff, and open-ended collection (v0.3 core upgrades)."""

from __future__ import annotations

import threading

import pytest

from battfeed import ErrorPolicy, Harvester, SourceFailure
from battfeed.cli import _parse_opts


class FlakySource:
    """Fails for the first ``fail_first`` polls, then yields one sample per poll."""

    name = "flaky"

    def __init__(self, fail_first: int = 0, fail_forever: bool = False) -> None:
        self.fail_first = fail_first
        self.fail_forever = fail_forever
        self.polls = 0

    def metadata(self):
        return {"kind": "flaky-test-source"}

    def poll(self):
        self.polls += 1
        if self.fail_forever or self.polls <= self.fail_first:
            raise ConnectionError(f"boom #{self.polls}")
        return [{"voltage_volt": 3.7, "current_ampere": 0.0}]


def test_transient_failures_are_retried_and_counted(fake_clock, list_sink) -> None:
    harvester = Harvester()
    source = FlakySource(fail_first=3)
    harvester.register(source)

    stats = harvester.collect(
        "flaky",
        duration_s=60.0,
        interval_s=1.0,
        sink=list_sink,
        clock=fake_clock,
        sleep=fake_clock.sleep,
    )

    assert stats.errors == 3
    assert stats.samples > 0
    assert list_sink.rows, "samples must flow after recovery"


def test_persistent_failure_raises_source_failure(fake_clock, list_sink) -> None:
    harvester = Harvester()
    harvester.register(FlakySource(fail_forever=True))
    policy = ErrorPolicy(max_consecutive_errors=3)

    with pytest.raises(SourceFailure) as excinfo:
        harvester.collect(
            "flaky",
            duration_s=60.0,
            interval_s=1.0,
            sink=list_sink,
            errors=policy,
            clock=fake_clock,
            sleep=fake_clock.sleep,
        )
    assert excinfo.value.consecutive == 3
    assert isinstance(excinfo.value.last_error, ConnectionError)
    assert list_sink.rows == []


def test_errors_none_fails_fast(fake_clock, list_sink) -> None:
    harvester = Harvester()
    harvester.register(FlakySource(fail_first=1))

    with pytest.raises(ConnectionError):
        harvester.collect(
            "flaky",
            duration_s=60.0,
            interval_s=1.0,
            sink=list_sink,
            errors=None,
            clock=fake_clock,
            sleep=fake_clock.sleep,
        )


def test_backoff_grows_exponentially_and_caps(fake_clock, list_sink) -> None:
    harvester = Harvester()
    harvester.register(FlakySource(fail_first=4))
    policy = ErrorPolicy(
        max_consecutive_errors=10, backoff_initial_s=1.0, backoff_factor=2.0, backoff_max_s=3.0
    )

    harvester.collect(
        "flaky",
        duration_s=1000.0,
        interval_s=5.0,
        sink=list_sink,
        errors=policy,
        clock=fake_clock,
        sleep=fake_clock.sleep,
    )

    # First sleeps are the error backoffs: 1, 2, then capped at 3.
    assert fake_clock.sleeps[:4] == [1.0, 2.0, 3.0, 3.0]


def test_policy_backoff_formula() -> None:
    policy = ErrorPolicy(backoff_initial_s=0.5, backoff_factor=3.0, backoff_max_s=10.0)
    assert policy.backoff(1) == 0.5
    assert policy.backoff(2) == 1.5
    assert policy.backoff(3) == 4.5
    assert policy.backoff(4) == 10.0  # capped


def test_unbounded_run_requires_stop_event(fake_clock, list_sink) -> None:
    harvester = Harvester()
    harvester.register(FlakySource())
    with pytest.raises(ValueError, match="stop"):
        harvester.collect(
            "flaky",
            duration_s=None,
            sink=list_sink,
            clock=fake_clock,
            sleep=fake_clock.sleep,
        )


def test_unbounded_run_ends_on_stop(fake_clock, list_sink) -> None:
    harvester = Harvester()

    stop = threading.Event()

    class StopAfterThree(FlakySource):
        name = "stopper"

        def poll(self):
            self.polls += 1
            if self.polls >= 3:
                stop.set()
            return [{"voltage_volt": 3.7, "current_ampere": 0.0}]

    harvester.register(StopAfterThree())
    stats = harvester.collect(
        "stopper",
        duration_s=None,
        interval_s=1.0,
        sink=list_sink,
        clock=fake_clock,
        sleep=fake_clock.sleep,
        stop=stop,
    )
    assert stats.samples == 3
    assert len(list_sink.rows) == 3


def test_parse_opts_json_coercion() -> None:
    opts = _parse_opts(
        [
            "slot=2",
            "rate=0.5",
            "enabled=true",
            "path=log.csv",
            'column_map={"V": "voltage_volt"}',
            'label="quoted string"',
        ]
    )
    assert opts == {
        "slot": 2,
        "rate": 0.5,
        "enabled": True,
        "path": "log.csv",
        "column_map": {"V": "voltage_volt"},
        "label": "quoted string",
    }


def test_parse_opts_rejects_malformed_pairs() -> None:
    with pytest.raises(SystemExit):
        _parse_opts(["no-equals-sign"])
