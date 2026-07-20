"""Replay tape tests: round-trip, time compression, recording -- zero hardware."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable

import pytest

from battfeed import StreamingSource
from battfeed.protocols import Sample
from battfeed.testing import ReplayReader, ReplayTape, TapeFrame, TapeRecorder


def make_tape() -> ReplayTape:
    tape = ReplayTape()
    tape.append(0.0, bytes([0x10, 0x0E, 0x74]), {"rssi": -61})  # 3700 mV
    tape.append(1.5, bytes([0x10, 0x0E, 0x6A]))  # 3690 mV
    tape.append(3600.0, bytes([0x10, 0x0E, 0x10]))  # an hour later: 3600 mV
    return tape


# -- tape format -------------------------------------------------------------
def test_tape_round_trip(tmp_path: Path) -> None:
    tape = make_tape()
    path = tmp_path / "shunt.tape.jsonl"
    tape.save(path)
    loaded = ReplayTape.load(path)
    assert loaded.frames == tape.frames
    assert len(loaded) == 3
    assert loaded.duration_s == 3600.0
    # The format on disk is plain JSONL with hex-encoded bytes.
    first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert first == {"t": 0.0, "data": "100e74", "meta": {"rssi": -61}}


def test_tape_load_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "gaps.jsonl"
    path.write_text('{"t": 0, "data": "00"}\n\n{"t": 1, "data": "01"}\n', encoding="utf-8")
    assert [frame.data for frame in ReplayTape.load(path)] == [b"\x00", b"\x01"]


@pytest.mark.parametrize(
    ("line", "complaint"),
    [
        ("not json", "not valid JSON"),
        ('["t", "data"]', "expected a JSON object"),
        ('{"t": 0.5}', "missing key"),
        ('{"data": "00"}', "missing key"),
        ('{"t": -1, "data": "00"}', "must be a number >= 0"),
        ('{"t": true, "data": "00"}', "must be a number >= 0"),
        ('{"t": 0, "data": 7}', "must be a hex string"),
        ('{"t": 0, "data": "zz"}', "not valid hex"),
        ('{"t": 0, "data": "00", "meta": [1]}', "must be a JSON object"),
    ],
)
def test_tape_load_rejects_mid_file_corruption(tmp_path: Path, line: str, complaint: str) -> None:
    # The bad line sits MID-file (a good line follows), so torn-tail
    # tolerance does not apply and the error is hard.
    path = tmp_path / "bad.jsonl"
    path.write_text(
        '{"t": 0, "data": "00"}\n' + line + '\n{"t": 9, "data": "02"}\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match=complaint) as excinfo:
        ReplayTape.load(path)
    assert "line 2" in str(excinfo.value)  # errors point at the offending line


def test_tape_load_tolerates_one_torn_final_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A recorder killed mid-write leaves a half line; the rest must survive."""
    path = tmp_path / "torn.jsonl"
    path.write_text(
        '{"t": 0, "data": "00"}\n{"t": 1, "data": "01"}\n{"t": 2, "da', encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING, logger="battfeed.testing.replay"):
        tape = ReplayTape.load(path)
    assert [frame.data for frame in tape] == [b"\x00", b"\x01"]
    assert any("torn tail" in record.message for record in caplog.records)


def test_tape_load_rejects_out_of_order_offsets(tmp_path: Path) -> None:
    path = tmp_path / "ooo.jsonl"
    path.write_text('{"t": 2, "data": "00"}\n{"t": 1, "data": "01"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="non-decreasing") as excinfo:
        ReplayTape.load(path)
    assert "line 2" in str(excinfo.value)


def test_tape_constructor_validates_offsets() -> None:
    with pytest.raises(ValueError, match=r"frame \[1\].*non-decreasing"):
        ReplayTape([TapeFrame(1.0, b"\x00"), TapeFrame(0.5, b"\x01")])
    with pytest.raises(ValueError, match=r"frame \[0\].*>= 0"):
        ReplayTape([TapeFrame(-0.1, b"\x00")])


def test_tape_append_rejects_unserializable_meta() -> None:
    tape = ReplayTape()
    with pytest.raises(ValueError, match="JSON-serializable"):
        tape.append(0.0, b"\x00", meta={"rssi": float("nan")})
    with pytest.raises(ValueError, match="JSON-serializable"):
        tape.append(0.0, b"\x00", meta={"obj": object()})
    assert len(tape) == 0  # nothing was appended by the failed calls


def test_tape_meta_int_keys_round_trip_to_str(tmp_path: Path) -> None:
    """Documented quirk: JSON objects have str keys, so {1: ...} loads as {"1": ...}."""
    tape = ReplayTape()
    tape.append(0.0, b"\x00", meta={1: "arbitration-id"})
    path = tmp_path / "intkey.jsonl"
    tape.save(path)
    assert ReplayTape.load(path).frames[0].meta == {"1": "arbitration-id"}


def test_tape_append_validates_offsets() -> None:
    tape = ReplayTape()
    tape.append(1.0, b"\x00")
    with pytest.raises(ValueError, match="non-decreasing"):
        tape.append(0.5, b"\x01")
    with pytest.raises(ValueError, match=">= 0"):
        ReplayTape().append(-0.1, b"\x00")


# -- replay ------------------------------------------------------------------
def test_instant_replay_compresses_time(fake_clock) -> None:
    """An hour-long tape replays with zero sleeps; offsets stay on the frames."""
    reader = ReplayReader(make_tape(), clock=fake_clock, sleep=fake_clock.sleep)
    seen: list[TapeFrame] = []
    delivered = reader.run(seen.append)
    assert delivered == 3
    assert [frame.t for frame in seen] == [0.0, 1.5, 3600.0]
    assert fake_clock.sleeps == []  # time compression: no sleeps at all


def test_paced_replay_sleeps_on_the_injected_clock(fake_clock) -> None:
    reader = ReplayReader(make_tape(), pace=2.0, clock=fake_clock, sleep=fake_clock.sleep)
    seen: list[float] = []
    reader.run(lambda frame: seen.append(frame.t))
    assert seen == [0.0, 1.5, 3600.0]
    # pace=2.0 halves the recorded timeline; the fake clock advances on sleep.
    assert fake_clock.sleeps == pytest.approx([0.75, 1799.25])
    assert fake_clock.now == pytest.approx(1800.0)


def test_replay_pace_must_be_positive() -> None:
    with pytest.raises(ValueError, match="pace"):
        ReplayReader(ReplayTape(), pace=0)


def test_replay_stops_when_asked(fake_clock) -> None:
    seen: list[TapeFrame] = []

    def stop_after_two() -> bool:
        return len(seen) >= 2

    delivered = ReplayReader(make_tape(), clock=fake_clock, sleep=fake_clock.sleep).run(
        seen.append, stop_after_two
    )
    assert delivered == 2
    assert len(seen) == 2


# -- recording ---------------------------------------------------------------
def test_recorder_tees_frames_into_a_loadable_tape(tmp_path: Path, fake_clock) -> None:
    path = tmp_path / "field-session.jsonl"
    forwarded: list[bytes] = []
    with TapeRecorder(path, clock=fake_clock) as recorder:
        callback = recorder.tee(forwarded.append)  # the "one flag" wrap of a live callback
        callback(b"\x01\x02")
        fake_clock.sleep(1.5)
        callback(b"\x03")
        recorder.record(b"\x04", meta={"rssi": -70})
        assert recorder.frames_recorded == 3
    assert forwarded == [b"\x01\x02", b"\x03"]  # the live pipeline still got every frame

    tape = ReplayTape.load(path)
    assert [(frame.t, frame.data) for frame in tape] == [
        (0.0, b"\x01\x02"),
        (1.5, b"\x03"),
        (1.5, b"\x04"),
    ]
    assert tape.frames[2].meta == {"rssi": -70}


def test_recorder_flushes_every_frame(tmp_path: Path, fake_clock) -> None:
    """An interrupted recording (never closed) still leaves a loadable tape."""
    path = tmp_path / "interrupted.jsonl"
    recorder = TapeRecorder(path, clock=fake_clock)
    recorder.record(b"\xaa")
    assert len(ReplayTape.load(path)) == 1  # readable before close
    recorder.close()
    recorder.close()  # idempotent
    with pytest.raises(ValueError, match="closed"):
        recorder.record(b"\xbb")


# -- driving a StreamingSource from a tape (the WP2.1 x WP2.2 seam) ----------
class ReplayedShuntSource(StreamingSource):
    """A minimal streaming source whose reader is a replay tape.

    The pattern future device sources reuse: decode raw frame bytes into BDF
    columns and derive ``test_time_second`` from the recorded offset, so an
    hour-long tape yields an hour-long timebase in milliseconds of test time.
    """

    def __init__(self, tape: ReplayTape) -> None:
        super().__init__("replayed-shunt")
        self._tape = tape

    def run_reader(self, emit: Callable[[Sample], None], should_stop: Callable[[], bool]) -> None:
        def decode(frame: TapeFrame) -> None:
            millivolts = int.from_bytes(frame.data[1:3], "big")
            emit({"test_time_second": frame.t, "voltage_volt": millivolts / 1000.0})

        ReplayReader(self._tape).run(decode, should_stop)
        # Stay connected after the tape ends, like a live listener would --
        # otherwise every silently restarted session would replay the tape again.
        while not should_stop():
            time.sleep(0.001)


def test_streaming_source_replays_a_tape_without_hardware() -> None:
    source = ReplayedShuntSource(make_tape())
    try:
        rows: list[Sample] = []
        deadline = time.monotonic() + 5.0
        while len(rows) < 3 and time.monotonic() < deadline:
            rows.extend(source.poll())
        assert [row["test_time_second"] for row in rows] == [0.0, 1.5, 3600.0]
        assert [row["voltage_volt"] for row in rows] == [3.7, 3.69, 3.6]
        assert source.received_total == 3
        assert source.dropped_total == 0
    finally:
        source.close()


def test_streaming_source_replay_stops_promptly_on_close() -> None:
    tape = ReplayTape()
    for i in range(10_000):
        tape.append(float(i), b"\x00\x00\x00")

    started = threading.Event()

    class SlowishReplay(ReplayedShuntSource):
        def run_reader(
            self, emit: Callable[[Sample], None], should_stop: Callable[[], bool]
        ) -> None:
            started.set()
            ReplayReader(self._tape).run(lambda frame: emit({"i": frame.t}), should_stop)

    source = SlowishReplay(tape)
    source.poll()
    assert started.wait(5.0)
    source.close()  # should_stop propagates into ReplayReader.run
    assert not source.reader_alive
