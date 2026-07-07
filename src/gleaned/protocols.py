"""Public contracts for gleaned data sources and sinks.

This module is the **stable seam** of gleaned: third-party collectors
implement :class:`DataSource`, output writers implement :class:`Sink`,
and everything else in the package is wiring between the two.

Both contracts use :class:`typing.Protocol` (structural typing), so an
implementation never needs to import or subclass anything from gleaned --
any object with the right attributes and methods satisfies the contract.

Sample shape
------------
A *sample* is a plain ``dict[str, float]`` whose keys are canonical
machine-readable BDF (Battery Data Format) column names of the form
``{quantity}_{unit}``, for example::

    {"test_time_second": 12.0, "voltage_volt": 3.71, "current_ampere": -0.002}

Sign convention (per the Battery Data Format specification): **positive
current charges the test object, negative current discharges it.**
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

__all__ = ["DataSource", "Sink"]


@runtime_checkable
class DataSource(Protocol):
    """A live source of battery samples.

    This protocol is the stable seam that third-party collectors implement.
    Implementations are structural: define ``name``, ``metadata()`` and
    ``poll()`` on any class and it *is* a ``DataSource`` -- no import or
    inheritance required. Register it with a :class:`gleaned.Harvester`
    directly, or expose it to the ``gleaned`` CLI through the
    ``"gleaned.sources"`` entry-point group.

    Optional hook
    -------------
    A source **may** additionally define ``close() -> None`` to release
    hardware handles, serial ports, network sessions, and so on. The hook is
    deliberately *not* part of the required protocol so that trivial sources
    stay trivial; callers (including the gleaned CLI) invoke it only when it
    is present.
    """

    name: str
    """Short unique identifier for the source, e.g. ``"simulator"``."""

    def metadata(self) -> Mapping[str, Any]:
        """Return a static description of the source.

        Called at most a handful of times per collection run (never in the
        hot loop). The mapping should be JSON-serialisable; it is recorded in
        the ``.meta.json`` sidecar written next to each BDF file.
        """
        ...

    def poll(self) -> list[dict[str, float]]:
        """Return zero or more NEW samples accumulated since the last call.

        Keys must be canonical BDF column names (``voltage_volt``,
        ``current_ampere``, ...); values must be numeric. A source MAY
        include ``test_time_second`` itself (e.g. when tailing an instrument
        log that records its own timebase); when absent, the harvester
        stamps each sample with the elapsed collection time.

        Must not block for longer than roughly one polling interval and must
        never return the same sample twice.
        """
        ...


@runtime_checkable
class Sink(Protocol):
    """A destination for collected samples.

    This protocol is the stable seam that output writers implement -- the
    harvester only ever calls ``write()`` and the owner of the sink calls
    ``close()`` exactly once when collection is finished. gleaned ships
    :class:`gleaned.BdfCsvSink`, which writes BDF CSV files; alternative
    sinks (message queues, databases, sockets) just need these two methods.
    """

    def write(self, rows: Iterable[Mapping[str, float]]) -> None:
        """Persist a batch of samples. May be called with an empty batch."""
        ...

    def close(self) -> None:
        """Flush and release resources. Must be idempotent."""
        ...
