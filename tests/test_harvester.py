from __future__ import annotations

import threading
from datetime import datetime

import pytest

from gleaned import Harvester


class StaticSource:
    """Minimal DataSource: one fixed sample per poll, no timestamps."""

    name = "static"

    def __init__(self) -> None:
        self.polls = 0

    def metadata(self):
        return {"source": self.name}

    def poll(self):
        self.polls += 1
        return [{"voltage_volt": 3.0, "current_ampere": -0.001}]


def make_harvester(source=None) -> Harvester:
    harvester = Harvester()
    harvester.register(source or StaticSource())
    return harvester


def test_collect_stamps_test_time_and_respects_duration(fake_clock, list_sink):
    harvester = make_harvester()
    stats = harvester.collect(
        "static",
        duration_s=3.0,
        interval_s=1.0,
        sink=list_sink,
        clock=fake_clock,
        sleep=fake_clock.sleep,
    )
    # Polls at t=0, 1, 2; duration elapses at t=3.
    assert stats.samples == 3
    assert stats.duration_s == pytest.approx(3.0)
    assert [row["test_time_second"] for row in list_sink.rows] == [0.0, 1.0, 2.0]
    assert all(sleep <= 1.0 for sleep in fake_clock.sleeps)
    # Harvester never closes the sink; its owner does.
    assert not list_sink.closed


def test_collect_preserves_source_supplied_time(fake_clock, list_sink):
    class TimedSource(StaticSource):
        name = "timed"

        def poll(self):
            return [{"test_time_second": 42.5, "voltage_volt": 3.1, "current_ampere": 0.0}]

    harvester = make_harvester(TimedSource())
    harvester.collect(
        "timed",
        duration_s=1.0,
        interval_s=1.0,
        sink=list_sink,
        clock=fake_clock,
        sleep=fake_clock.sleep,
    )
    assert list_sink.rows[0]["test_time_second"] == 42.5


def test_collect_stops_on_stop_event(fake_clock, list_sink):
    stop = threading.Event()

    class StoppingSource(StaticSource):
        name = "stopping"

        def poll(self):
            rows = super().poll()
            if self.polls == 2:
                stop.set()
            return rows

    harvester = make_harvester(StoppingSource())
    stats = harvester.collect(
        "stopping",
        duration_s=1000.0,
        interval_s=1.0,
        sink=list_sink,
        clock=fake_clock,
        sleep=fake_clock.sleep,
        stop=stop,
    )
    assert stats.samples == 2
    assert fake_clock.now < 3  # ended long before the nominal duration


def test_collect_unknown_source_raises_keyerror(fake_clock, list_sink):
    harvester = make_harvester()
    with pytest.raises(KeyError, match="No source registered"):
        harvester.collect(
            "missing",
            duration_s=1.0,
            sink=list_sink,
            clock=fake_clock,
            sleep=fake_clock.sleep,
        )


def test_status_reflects_registration_and_collection(fake_clock, list_sink):
    harvester = Harvester()
    assert harvester.status("static") == {
        "registered": False,
        "last_poll_at": None,
        "samples_collected": 0,
    }
    source = StaticSource()
    harvester.register(source)
    assert harvester.status("static")["registered"] is True
    assert "static" in harvester.sources

    harvester.collect(
        "static",
        duration_s=2.0,
        interval_s=1.0,
        sink=list_sink,
        clock=fake_clock,
        sleep=fake_clock.sleep,
    )
    status = harvester.status("static")
    assert status["samples_collected"] == 2
    assert status["last_poll_at"] is not None


def test_collect_stats_reports_source_columns_and_start(fake_clock, list_sink):
    harvester = make_harvester()
    stats = harvester.collect(
        "static",
        duration_s=1.0,
        interval_s=1.0,
        sink=list_sink,
        clock=fake_clock,
        sleep=fake_clock.sleep,
    )
    assert stats.source == "static"
    assert stats.columns == ["current_ampere", "test_time_second", "voltage_volt"]
    datetime.fromisoformat(stats.started_at)  # valid ISO 8601
