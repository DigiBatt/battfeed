"""Public contracts for battfeed data sources and sinks.

This module is the **stable seam** of battfeed: third-party collectors
implement :class:`DataSource`, output writers implement :class:`Sink`,
and everything else in the package is wiring between the two.

Both contracts use :class:`typing.Protocol` (structural typing), so an
implementation never needs to import or subclass anything from battfeed --
any object with the right attributes and methods satisfies the contract.
This is deliberate: commercial platforms can ship proprietary sources and
sinks that plug into battfeed without depending on its internals.

Sample shape
------------
A *sample* is a plain ``dict`` whose keys are canonical machine-readable
BDF (Battery Data Format) column names of the form ``{quantity}_{unit}``,
for example::

    {"test_time_second": 12.0, "voltage_volt": 3.71, "current_ampere": -0.002}

Values are normally numeric. String values are permitted for auxiliary
columns (e.g. a charge status flag) -- they are written to the CSV as-is,
but note that strict BDF validation flags columns outside the canonical
vocabulary. Slow-changing facts (device model, chemistry, firmware)
belong in :meth:`DataSource.metadata`, not in every sample.

Sign convention (per the Battery Data Format specification): **positive
current charges the test object, negative current discharges it.**
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Protocol, Union, runtime_checkable

__all__ = ["DataSource", "Sink", "Sample", "SampleValue"]

SampleValue = Union[float, int, str]
"""A single measured value; numeric for canonical BDF columns."""

Sample = dict[str, SampleValue]
"""One reading: canonical BDF column names mapped to values."""


@runtime_checkable
class DataSource(Protocol):
    """A live source of battery samples.

    This protocol is the stable seam that third-party collectors implement.
    Implementations are structural: define ``name``, ``metadata()`` and
    ``poll()`` on any class and it *is* a ``DataSource`` -- no import or
    inheritance required. Register it with a :class:`battfeed.Harvester`
    directly, or expose it to the ``battfeed`` CLI through the
    ``"battfeed.sources"`` entry-point group.

    Error handling
    --------------
    ``poll()`` MAY raise when the underlying device is briefly unreachable;
    the harvester's :class:`battfeed.ErrorPolicy` retries with backoff, so
    sources should NOT implement their own retry loops. A source should
    only swallow errors it can genuinely resolve better itself (e.g. one
    bad frame out of several channels).

    Optional hooks
    --------------
    ``close() -> None`` releases hardware handles, serial ports, network
    sessions, and so on; callers invoke it when present. A classmethod
    ``availability() -> str | None`` may report why the source cannot run
    here (missing optional dependency, wrong platform); the CLI uses it to
    annotate ``battfeed sources``. Neither is part of the required protocol,
    so trivial sources stay trivial.
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

    def poll(self) -> list[Sample]:
        """Return zero or more NEW samples accumulated since the last call.

        Keys must be canonical BDF column names (``voltage_volt``,
        ``current_ampere``, ...). A source MAY include ``test_time_second``
        itself (e.g. when tailing an instrument log that records its own
        timebase); when absent, the harvester stamps each sample with the
        elapsed collection time.

        Must not block for longer than roughly one polling interval and must
        never return the same sample twice.
        """
        ...


@runtime_checkable
class Sink(Protocol):
    """A destination for collected samples.

    This protocol is the stable seam that output writers implement -- the
    harvester only ever calls ``write()`` and the owner of the sink calls
    ``close()`` exactly once when collection is finished. battfeed ships
    :class:`battfeed.BdfCsvSink`, which writes BDF CSV files; alternative
    sinks (message queues, databases, platform ingest APIs) just need these
    two methods.
    """

    def write(self, rows: Iterable[Mapping[str, SampleValue]]) -> None:
        """Persist a batch of samples. May be called with an empty batch."""
        ...

    def close(self) -> None:
        """Flush and release resources. Must be idempotent."""
        ...
