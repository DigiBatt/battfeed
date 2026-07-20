"""Contract test kit: assert that a source honors the ``DataSource`` seam.

battfeed's universality depends on *third parties* shipping sources, and the
seam is structural (:class:`battfeed.DataSource` is a ``typing.Protocol``) --
nothing forces an implementation to be correct at import time. This module is
the executable half of the contract: call :func:`check_source` from your own
test suite against your source (backed by a mock transport or a replay tape,
never live hardware) and it asserts the essentials every battfeed pipeline
relies on.

What is checked
---------------
The structural surface (``name`` / ``metadata()`` / ``poll()`` and the
optional ``close()`` / ``availability()`` hooks); strict JSON
serializability of metadata *and* samples (``allow_nan=False`` -- NaN and
Infinity are rejected, because sidecars and BDF consumers cannot represent
them); sample shape (non-empty str keys; int/float/str values; ``bool`` is
rejected everywhere -- ``isinstance(True, int)`` holds in Python, but a bool
in a measurement column is always a bug); ``test_time_second`` numeric and
non-negative; freshness (the same dict object must not appear twice in one
batch or be recycled across polls -- downstream code mutates rows in place);
and the reserved-key discipline: a sample carrying ``series_id`` / ``run_id``
must also carry its own ``test_time_second`` (invariant I5 -- the harvester's
shared elapsed-collection stamp is wrong for objects that appear mid-run).

Deliberately NOT checked
------------------------
Honesty about the kit's blind spots, so a green check is not oversold:

* **Blocking polls** -- ``poll()`` must not block for longer than roughly one
  polling interval; a checker cannot draw that line for hardware it has
  never seen.
* **Duplicate-content batches** -- rows that are *equal by value* are legal
  (an instrument may genuinely repeat a reading); only object identity
  (aliasing) is checked.
* **Mutation of previously returned rows** -- a source that hands out fresh
  dicts but later mutates the old ones passes; catching it would require
  deep-copy snapshots of every batch, and downstream consumers should not be
  reading old batches anyway.
* **Cross-poll ``test_time_second`` monotonicity** -- runs may legitimately
  restart the clock at segment boundaries; validating monotonicity requires
  run semantics the kit cannot know. Validate emitted files with
  ``batterydf`` instead.
* **Shared-timebase I5-in-spirit violations** -- a routing source that stamps
  *every* series from one shared clock satisfies the per-sample rule checked
  here while still violating I5's intent; only a test with two series
  appearing at different times can catch that.
* **Column vocabulary, sign conventions, units** -- these need domain
  knowledge of your device; verify signs against real charge/discharge
  behavior and validate files with ``batterydf``.

Failures raise :class:`AssertionError` with a message naming the violated
rule, so a bare ``check_source(MySource(...))`` inside any test function is a
complete contract test.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from ..protocols import RESERVED_KEYS

__all__ = ["check_source"]


def _check(condition: bool, message: str) -> None:
    # Explicit raise rather than `assert`: the kit must keep checking under
    # `python -O`, where assert statements are stripped.
    if not condition:
        raise AssertionError(f"check_source: {message}")


def check_source(source: Any, *, polls: int = 3) -> None:
    """Assert the ``DataSource`` protocol essentials on a live instance.

    Polls the source ``polls`` times, so drive it with a mock transport or a
    replay tape -- never hardware. If the source has a ``close()`` hook it is
    called (twice -- battfeed's own sources document close() as idempotent,
    and the CLI relies on that being safe). See the module docstring for the
    full list of what is checked and, just as important, what is
    **deliberately not checked** (blocking polls, duplicate-content batches,
    cross-poll ``test_time_second`` monotonicity, shared-timebase I5
    violations, vocabulary/signs/units).

    Args:
        source: The instance to check (any object; failures explain what is
            missing).
        polls: Number of ``poll()`` calls to sample-check (>= 1).

    Raises:
        AssertionError: with a message naming the violated contract rule.
    """
    _check(polls >= 1, f"polls must be >= 1, got {polls}")

    name = getattr(source, "name", None)
    _check(
        isinstance(name, str) and bool(name.strip()),
        "source.name must be a non-empty, non-whitespace str",
    )

    metadata = source.metadata()
    _check(
        isinstance(metadata, Mapping),
        f"metadata() must return a mapping, got {type(metadata).__name__}",
    )
    for key in metadata:
        _check(
            isinstance(key, str),
            f"metadata() keys must be str, got {key!r} -- json.dumps would silently "
            "coerce it, renaming the key in every sidecar",
        )
    try:
        json.dumps(dict(metadata), allow_nan=False)
    except (TypeError, ValueError) as exc:
        _check(
            False,
            "metadata() must be JSON-serializable with finite numbers "
            f"(sidecars record it verbatim; NaN/Infinity are not valid JSON): {exc}",
        )

    availability = getattr(type(source), "availability", None)
    if availability is not None:
        _check(callable(availability), "availability must be callable when present")
        reason = availability()
        _check(
            reason is None or isinstance(reason, str),
            f"availability() must return None or str, got {type(reason).__name__}",
        )

    # Hold a reference to the previous batch so its rows cannot be garbage
    # collected -- otherwise id() values could be legitimately reused and the
    # cross-poll aliasing check would false-positive.
    previous_batch: list[Any] = []
    for poll_no in range(1, polls + 1):
        batch = source.poll()
        _check(
            isinstance(batch, list),
            f"poll() must return a list (poll #{poll_no} returned {type(batch).__name__})",
        )
        previous_ids = {id(row) for row in previous_batch}
        seen_ids: set[int] = set()
        for i, row in enumerate(batch):
            where = f"poll #{poll_no} sample [{i}]"
            _check(
                isinstance(row, dict),
                f"{where}: samples must be dicts, got {type(row).__name__}",
            )
            _check(
                id(row) not in seen_ids,
                f"{where}: the same dict object appears twice in one batch -- "
                "samples must be distinct dicts (downstream code mutates rows)",
            )
            _check(
                id(row) not in previous_ids,
                f"{where}: poll() returned the same dict object as the previous "
                "poll -- samples must be fresh dicts, not a recycled buffer",
            )
            seen_ids.add(id(row))
            _check_row(row, where)
        previous_batch = batch

    close = getattr(source, "close", None)
    if close is not None:
        _check(callable(close), "close must be callable when present")
        close()
        close()  # idempotency is part of the documented close() contract


def _check_row(row: dict[str, Any], where: str) -> None:
    """Shape, value types, strict-JSON finiteness, and routing discipline."""
    for key, value in row.items():
        _check(
            isinstance(key, str) and bool(key),
            f"{where}: sample keys must be non-empty str, got {key!r}",
        )
        _check(
            not isinstance(value, bool),
            f"{where}: value of {key!r} is a bool -- booleans are not measurement "
            "values (encode state as an explicit str or 0/1 int instead)",
        )
        _check(
            isinstance(value, (int, float, str)),
            f"{where}: value of {key!r} must be int, float or str, got {type(value).__name__}",
        )
    try:
        json.dumps(row, allow_nan=False)
    except ValueError as exc:
        _check(
            False,
            f"{where}: sample values must be finite -- NaN/Infinity are not valid "
            f"JSON and not valid BDF ({exc})",
        )
    _check_routing_discipline(row, where)


def _check_routing_discipline(row: dict[str, Any], where: str) -> None:
    """Reserved keys are str, and routed samples own their timebase (I5)."""
    present = [key for key in RESERVED_KEYS if key in row]
    for key in present:
        _check(
            isinstance(row[key], str),
            f"{where}: reserved routing key {key!r} must be a str, got {type(row[key]).__name__}",
        )
    if present:
        _check(
            "test_time_second" in row,
            f"{where}: a sample carrying {present} must supply its own test_time_second "
            "(invariant I5: the harvester's shared elapsed-collection stamp is wrong for "
            "objects that appear mid-run)",
        )
    time_value = row.get("test_time_second")
    if time_value is not None:
        _check(
            isinstance(time_value, (int, float)) and not isinstance(time_value, bool),
            f"{where}: test_time_second must be numeric, got {time_value!r}",
        )
        _check(
            bool(time_value >= 0),
            f"{where}: test_time_second must be >= 0, got {time_value!r}",
        )
