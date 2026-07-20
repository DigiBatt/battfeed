"""check_source tests: the kit passes real sources and names each violation."""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from battfeed.sources.mc3000 import Mc3000Source
from battfeed.sources.simulator import SimulatedCellSource
from battfeed.testing import check_source


class GoodSource:
    """A minimal conforming third-party source (no battfeed inheritance)."""

    name = "good"

    def __init__(self) -> None:
        self.closes = 0

    def metadata(self) -> dict[str, Any]:
        return {"source": self.name, "kind": "test"}

    def poll(self) -> list[dict[str, Any]]:
        return [{"voltage_volt": 3.7, "current_ampere": -0.1, "status": "discharging"}]

    def close(self) -> None:
        self.closes += 1


class GoodRoutingSource(GoodSource):
    """Routing keys plus a source-owned timebase: the invariant-I5 shape."""

    name = "good-routing"

    def poll(self) -> list[dict[str, Any]]:
        return [
            {
                "series_id": "pack-A",
                "run_id": "flight-1",
                "test_time_second": 0.5,
                "voltage_volt": 11.1,
            }
        ]


def test_passes_the_builtin_simulator() -> None:
    check_source(SimulatedCellSource())


def test_passes_mc3000_on_the_mock_transport() -> None:
    check_source(Mc3000Source(transport="mock"))


def test_passes_a_minimal_third_party_source_and_closes_it() -> None:
    source = GoodSource()
    check_source(source)
    assert source.closes == 2  # close() is exercised twice: it must be idempotent


def test_passes_a_routing_source_with_its_own_timebase() -> None:
    check_source(GoodRoutingSource())


def test_polls_must_be_at_least_one() -> None:
    with pytest.raises(AssertionError, match="polls"):
        check_source(GoodSource(), polls=0)


def _broken(**overrides: Any) -> GoodSource:
    source = GoodSource()
    for attr, value in overrides.items():
        setattr(source, attr, value)
    return source


def test_rejects_missing_or_empty_name() -> None:
    class Nameless:
        def metadata(self) -> dict[str, Any]:
            return {}

        def poll(self) -> list[dict[str, Any]]:
            return []

    with pytest.raises(AssertionError, match="name"):
        check_source(Nameless())
    with pytest.raises(AssertionError, match="name"):
        check_source(_broken(name=""))
    with pytest.raises(AssertionError, match="name"):
        check_source(_broken(name=7))


def test_rejects_unserializable_metadata() -> None:
    source = _broken(metadata=lambda: {"when": datetime.datetime(2026, 7, 19)})
    with pytest.raises(AssertionError, match="JSON-serializable"):
        check_source(source)


def test_rejects_non_mapping_metadata() -> None:
    with pytest.raises(AssertionError, match="mapping"):
        check_source(_broken(metadata=lambda: ["not", "a", "mapping"]))


def test_rejects_non_list_poll() -> None:
    with pytest.raises(AssertionError, match="must return a list"):
        check_source(_broken(poll=lambda: iter([])))


def test_rejects_non_dict_samples() -> None:
    with pytest.raises(AssertionError, match="must be dicts"):
        check_source(_broken(poll=lambda: [("voltage_volt", 3.7)]))


def test_rejects_non_string_sample_keys() -> None:
    with pytest.raises(AssertionError, match="keys must be non-empty str"):
        check_source(_broken(poll=lambda: [{1: 3.7}]))


def test_rejects_non_scalar_sample_values() -> None:
    with pytest.raises(AssertionError, match="int, float or str"):
        check_source(_broken(poll=lambda: [{"voltage_volt": [3.7]}]))


def test_rejects_routing_keys_without_test_time_second() -> None:
    source = _broken(poll=lambda: [{"series_id": "pack-A", "voltage_volt": 11.1}])
    with pytest.raises(AssertionError, match="invariant I5"):
        check_source(source)


def test_rejects_non_string_routing_values() -> None:
    source = _broken(poll=lambda: [{"run_id": 3, "test_time_second": 0.0, "voltage_volt": 11.1}])
    with pytest.raises(AssertionError, match="run_id"):
        check_source(source)


def test_rejects_non_numeric_test_time() -> None:
    with pytest.raises(AssertionError, match="test_time_second must be numeric"):
        check_source(_broken(poll=lambda: [{"test_time_second": "0.0"}]))
    # A bool test_time_second now falls to the general bool rejection first.
    with pytest.raises(AssertionError, match="bool"):
        check_source(_broken(poll=lambda: [{"test_time_second": True}]))


def test_rejects_negative_test_time() -> None:
    with pytest.raises(AssertionError, match="test_time_second must be >= 0"):
        check_source(_broken(poll=lambda: [{"test_time_second": -0.5, "voltage_volt": 3.7}]))


def test_rejects_bool_sample_values_in_any_column() -> None:
    # isinstance(True, int) holds in Python; the checker must not be fooled.
    with pytest.raises(AssertionError, match="bool"):
        check_source(_broken(poll=lambda: [{"charging": True}]))
    with pytest.raises(AssertionError, match="bool"):
        check_source(_broken(poll=lambda: [{"voltage_volt": False}]))


def test_rejects_nan_and_infinity_sample_values() -> None:
    with pytest.raises(AssertionError, match="finite"):
        check_source(_broken(poll=lambda: [{"voltage_volt": float("nan")}]))
    with pytest.raises(AssertionError, match="finite"):
        check_source(_broken(poll=lambda: [{"current_ampere": float("inf")}]))


def test_rejects_nan_in_metadata() -> None:
    with pytest.raises(AssertionError, match="finite"):
        check_source(_broken(metadata=lambda: {"calibration": float("nan")}))


def test_rejects_non_string_metadata_keys() -> None:
    # json.dumps would silently coerce {1: ...} to {"1": ...} in the sidecar.
    with pytest.raises(AssertionError, match="metadata.*keys must be str"):
        check_source(_broken(metadata=lambda: {1: "coerced"}))


def test_rejects_whitespace_only_name() -> None:
    with pytest.raises(AssertionError, match="name"):
        check_source(_broken(name="   "))


def test_rejects_aliased_rows_within_a_batch() -> None:
    row = {"voltage_volt": 3.7}
    with pytest.raises(AssertionError, match="appears twice in one batch"):
        check_source(_broken(poll=lambda: [row, row]))


def test_rejects_row_object_recycled_across_polls() -> None:
    class Recycler(GoodSource):
        name = "recycler"

        def __init__(self) -> None:
            super().__init__()
            self._row = {"voltage_volt": 3.7}

        def poll(self) -> list[dict[str, Any]]:
            self._row["voltage_volt"] = 3.6  # mutate-in-place buffer reuse
            return [self._row]

    with pytest.raises(AssertionError, match="same dict object as the previous poll"):
        check_source(Recycler())


def test_docstring_names_the_deliberate_blind_spots() -> None:
    """The kit must not oversell a green check: its blind spots are documented."""
    import battfeed.testing.contract as contract

    doc = contract.__doc__ or ""
    assert "Deliberately NOT checked" in doc
    for blind_spot in (
        "Blocking polls",
        "Duplicate-content batches",
        "monotonicity",
        "Shared-timebase",
    ):
        assert blind_spot in doc, f"docstring must name blind spot: {blind_spot}"


def test_rejects_broken_availability() -> None:
    class BadAvailability(GoodSource):
        @classmethod
        def availability(cls) -> int:
            return 404

    with pytest.raises(AssertionError, match="availability"):
        check_source(BadAvailability())


def test_streaming_source_subclass_satisfies_the_contract() -> None:
    """WP2.1 x WP2.2: a replay-driven StreamingSource passes the kit."""
    from battfeed import StreamingSource
    from battfeed.protocols import Sample
    from battfeed.testing import ReplayReader, ReplayTape

    tape = ReplayTape()
    tape.append(0.0, b"\x0e\x74")
    tape.append(1.0, b"\x0e\x6a")

    class TapeSource(StreamingSource):
        def __init__(self) -> None:
            super().__init__("tape")

        def run_reader(self, emit: Any, should_stop: Any) -> None:
            def decode(frame: Any) -> None:
                sample: Sample = {
                    "test_time_second": frame.t,
                    "voltage_volt": int.from_bytes(frame.data, "big") / 1000.0,
                }
                emit(sample)

            ReplayReader(tape).run(decode, should_stop)

    check_source(TapeSource())
