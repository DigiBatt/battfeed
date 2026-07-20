"""Record/replay tapes: hardware-free fixtures for streaming sources.

Streaming hardware (BLE advertisements, CAN frames, MQTT messages) is awkward
to develop against -- the primary development machine is Windows while the
tooling is Linux-first, devices are not on every desk, and CI has neither.
The replay-first workflow: record raw frames from a live session *once* into
a **tape**, commit the tape, and drive every test and most development from
the tape with no hardware and no waiting.

Tape format
-----------
A tape is a JSONL file (one JSON object per line, UTF-8), stable and
diff-friendly on purpose -- tapes are committed fixtures::

    {"t": 0.0,   "data": "10099f...", "meta": {"rssi": -61}}
    {"t": 1.024, "data": "10099e..."}

* ``t``    -- seconds since the start of the recording (float, >= 0,
  non-decreasing).
* ``data`` -- the raw frame bytes, hex-encoded. What a "frame" is belongs to
  the recording source (a BLE advertisement payload, a CAN frame, an MQTT
  message body); the tape does not interpret it.
* ``meta`` -- optional JSON object of per-frame context (RSSI, CAN arbitration
  id, topic, ...). Anonymize identifiers (MAC addresses, keys) before
  committing a field recording.

Replaying compresses time by default: :class:`ReplayReader` delivers frames
as fast as the consumer accepts them, so an hour-long tape replays in
milliseconds -- the offsets stay available on each frame for sources that
derive ``test_time_second`` from them. Pacing (for demos or soak tests) is
opt-in and clock-injectable, so even paced tests can run on a fake clock with
zero real sleeps.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Callable, Iterable, Iterator, Mapping

__all__ = ["ReplayReader", "ReplayTape", "TapeFrame", "TapeRecorder"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TapeFrame:
    """One recorded frame: a time offset, raw bytes, optional context."""

    t: float
    """Seconds since the start of the recording."""

    data: bytes
    """Raw frame bytes, exactly as received from the transport."""

    meta: Mapping[str, Any] | None = None
    """Optional per-frame context (RSSI, arbitration id, topic, ...)."""


class ReplayTape:
    """An ordered sequence of :class:`TapeFrame` with JSONL load/save.

    Frame offsets must be >= 0 and non-decreasing however the tape is built
    (constructor, :meth:`append` or :meth:`load`) -- a replayed timeline that
    jumps backwards would silently corrupt any ``test_time_second`` derived
    from it.
    """

    def __init__(self, frames: Iterable[TapeFrame] = ()) -> None:
        self.frames: list[TapeFrame] = list(frames)
        previous = 0.0
        for i, frame in enumerate(self.frames):
            if frame.t < 0:
                raise ValueError(f"frame [{i}]: offset must be >= 0, got {frame.t}")
            if frame.t < previous:
                raise ValueError(
                    f"frame [{i}]: offsets must be non-decreasing ({frame.t} after {previous})"
                )
            previous = frame.t

    def append(self, t: float, data: bytes, meta: Mapping[str, Any] | None = None) -> None:
        """Append one frame; offsets must be >= 0 and non-decreasing.

        ``meta`` is validated eagerly: it must be JSON-serializable with
        finite numbers (``allow_nan=False``), so a bad recording fails at the
        recording site instead of poisoning every later load. Note that JSON
        objects have string keys: an int key like ``{1: "a"}`` serializes
        fine but **round-trips as** ``{"1": "a"}``.
        """
        if t < 0:
            raise ValueError(f"frame offset must be >= 0, got {t}")
        if self.frames and t < self.frames[-1].t:
            raise ValueError(f"frame offsets must be non-decreasing: {t} after {self.frames[-1].t}")
        if meta:
            try:
                json.dumps(dict(meta), allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"frame meta must be JSON-serializable with finite numbers "
                    f"(no NaN/Infinity; int keys round-trip to str): {exc}"
                ) from exc
        self.frames.append(TapeFrame(float(t), bytes(data), dict(meta) if meta else None))

    def __len__(self) -> int:
        return len(self.frames)

    def __iter__(self) -> Iterator[TapeFrame]:
        return iter(self.frames)

    @property
    def duration_s(self) -> float:
        """Offset of the last frame (0.0 for an empty tape)."""
        return self.frames[-1].t if self.frames else 0.0

    @classmethod
    def load(cls, path: str | Path) -> ReplayTape:
        """Read a JSONL tape file; malformed lines raise with the line number.

        The whole tape is read into memory -- tapes are committed test
        fixtures, not archives; keep them small.

        One concession to reality: recorders flush line by line, so an OS
        crash or power loss can leave a torn, half-written **final** line. A
        malformed *last* line is therefore skipped with a warning instead of
        voiding the tape; malformed lines anywhere else (and out-of-order
        offsets anywhere, including the last line) stay hard errors, because
        they mean corruption or hand-editing, not a torn tail.
        """
        with open(path, encoding="utf-8") as fh:
            lines = [(lineno, raw.strip()) for lineno, raw in enumerate(fh, 1)]
        content = [(lineno, line) for lineno, line in lines if line]
        frames: list[TapeFrame] = []
        for index, (lineno, line) in enumerate(content):
            try:
                frame = _parse_frame(line, path, lineno)
            except ValueError as exc:
                if index == len(content) - 1:
                    logger.warning(
                        "%s: ignoring unparseable final line %d -- torn tail from an "
                        "interrupted recording? (%s)",
                        path,
                        lineno,
                        exc,
                    )
                    break
                raise
            if frames and frame.t < frames[-1].t:
                raise ValueError(
                    f"{path}: line {lineno}: frame offsets must be non-decreasing "
                    f"({frame.t} after {frames[-1].t})"
                )
            frames.append(frame)
        return cls(frames)

    def save(self, path: str | Path) -> None:
        """Write the tape as JSONL (the format committed fixtures use)."""
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            for frame in self.frames:
                fh.write(_frame_line(frame.t, frame.data, frame.meta))


def _frame_line(t: float, data: bytes, meta: Mapping[str, Any] | None) -> str:
    obj: dict[str, Any] = {"t": round(t, 6), "data": data.hex()}
    if meta:
        obj["meta"] = dict(meta)
    try:
        return json.dumps(obj, separators=(",", ":"), allow_nan=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"frame meta must be JSON-serializable with finite numbers "
            f"(no NaN/Infinity; int keys round-trip to str): {exc}"
        ) from exc


def _parse_frame(line: str, path: str | Path, lineno: int) -> TapeFrame:
    where = f"{path}: line {lineno}"
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{where}: not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError(f"{where}: expected a JSON object, got {type(obj).__name__}")
    missing = {"t", "data"} - obj.keys()
    if missing:
        raise ValueError(f"{where}: tape frame is missing key(s) {sorted(missing)}")
    t = obj["t"]
    if not isinstance(t, (int, float)) or isinstance(t, bool) or t < 0:
        raise ValueError(f"{where}: 't' must be a number >= 0, got {t!r}")
    data = obj["data"]
    if not isinstance(data, str):
        raise ValueError(f"{where}: 'data' must be a hex string, got {type(data).__name__}")
    try:
        blob = bytes.fromhex(data)
    except ValueError as exc:
        raise ValueError(f"{where}: 'data' is not valid hex: {data!r}") from exc
    meta = obj.get("meta")
    if meta is not None and not isinstance(meta, dict):
        raise ValueError(f"{where}: 'meta' must be a JSON object when present")
    return TapeFrame(float(t), blob, meta)


class ReplayReader:
    """Deliver a tape's frames to a handler -- the reader loop of a replay source.

    Built to slot straight into
    :meth:`battfeed.StreamingSource.run_reader`: pass the base's
    ``should_stop`` through and decode each frame into ``emit``::

        class ReplayedShuntSource(StreamingSource):
            def __init__(self, tape: ReplayTape) -> None:
                super().__init__("replayed-shunt")
                self._tape = tape

            def run_reader(self, emit, should_stop):
                ReplayReader(self._tape).run(
                    lambda frame: emit(self._decode(frame)), should_stop
                )

    By default (``pace=None``) frames are delivered as fast as the handler
    accepts them -- **time compression**: an hour of recording replays in
    milliseconds with zero sleeps, and the recorded offset stays available as
    ``frame.t`` for sources that derive ``test_time_second`` from it.
    ``pace=1.0`` replays on the recorded timeline (2.0 = twice as fast, ...)
    using the injectable ``clock``/``sleep``, so even paced replay is testable
    on a fake clock without real waiting.
    """

    def __init__(
        self,
        tape: ReplayTape,
        *,
        pace: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if pace is not None and pace <= 0:
            raise ValueError(f"pace must be positive (or None for instant replay), got {pace}")
        self._tape = tape
        self._pace = pace
        self._clock = clock
        self._sleep = sleep

    def run(
        self,
        handle: Callable[[TapeFrame], None],
        should_stop: Callable[[], bool] | None = None,
    ) -> int:
        """Feed every frame to ``handle``; return the number delivered.

        Checks ``should_stop`` before each frame (and before each paced
        sleep), so a :class:`~battfeed.StreamingSource` shutdown ends the
        replay promptly.
        """
        stopped = should_stop or (lambda: False)
        start = self._clock()
        delivered = 0
        for frame in self._tape:
            if stopped():
                break
            if self._pace is not None:
                delay = (start + frame.t / self._pace) - self._clock()
                if delay > 0:
                    self._sleep(delay)
                if stopped():
                    break
            handle(frame)
            delivered += 1
        return delivered


class TapeRecorder:
    """Tee raw frames from a live reader into a tape file, line by line.

    Turning a field session into a committed fixture should cost one flag in
    the recording tool: wrap the existing frame callback with :meth:`tee`
    (or call :meth:`record` directly from it) and every frame is appended to
    the JSONL tape with its offset from the recorder's start. Each line is
    flushed as written, so an interrupted session still leaves a loadable
    tape of everything received so far.

    Usable as a context manager; ``close()`` is idempotent.
    """

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._path = Path(path)
        self._clock = clock
        self._fh: IO[str] | None = open(self._path, "w", encoding="utf-8", newline="\n")
        self._t0 = clock()
        self.frames_recorded = 0

    def record(self, data: bytes, meta: Mapping[str, Any] | None = None) -> None:
        """Append one frame, stamped with the offset since the recorder opened."""
        if self._fh is None:
            raise ValueError(f"TapeRecorder({str(self._path)!r}) is closed")
        t = max(0.0, self._clock() - self._t0)
        self._fh.write(_frame_line(t, bytes(data), meta))
        self._fh.flush()  # an interrupted recording keeps everything so far
        self.frames_recorded += 1

    def tee(self, callback: Callable[[bytes], None]) -> Callable[[bytes], None]:
        """Wrap a live frame callback so every frame is recorded, then forwarded."""

        def recorded(data: bytes) -> None:
            self.record(data)
            callback(data)

        return recorded

    def close(self) -> None:
        """Flush and close the tape file. Idempotent."""
        fh, self._fh = self._fh, None
        if fh is not None:
            fh.close()

    def __enter__(self) -> TapeRecorder:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
