"""Shared test fakes: no wall-clock, no hardware, no network."""

from __future__ import annotations

import pytest


class FakeClock:
    """Monotonic clock that only advances when its sleep() is called."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ListSink:
    """Sink that keeps every row in memory."""

    def __init__(self) -> None:
        self.rows: list[dict[str, float]] = []
        self.closed = False

    def write(self, rows) -> None:
        self.rows.extend(dict(row) for row in rows)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def list_sink() -> ListSink:
    return ListSink()
