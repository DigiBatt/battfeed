"""RoutingSink: per-(series, run) demultiplexing, rotation, sanitization, I5."""

from __future__ import annotations

import csv
import datetime
import hashlib
import json
import logging
from pathlib import Path

import pytest

from battfeed import Harvester, RoutingSink, Sink
from battfeed.sinks.routing import sanitize_cell_name

FIXED_DATE = datetime.date(2026, 7, 19)


def fixed_today() -> datetime.date:
    return FIXED_DATE


def sample(series=None, run=None, t=0.0, v=3.7, i=0.0, **extra):
    row = {"test_time_second": t, "voltage_volt": v, "current_ampere": i, **extra}
    if series is not None:
        row["series_id"] = series
    if run is not None:
        row["run_id"] = run
    return row


def read_rows(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_sidecar(path: Path) -> dict:
    sidecar = path.with_name(path.name.removesuffix(".bdf.csv") + ".meta.json")
    return json.loads(sidecar.read_text(encoding="utf-8"))


class RecordingSink:
    """Child-sink stand-in that never touches disk."""

    def __init__(self, path, *, metadata):
        self.path = Path(path)
        self.metadata = dict(metadata)
        self.rows: list[dict] = []
        self.closed = False

    def write(self, rows) -> None:
        assert not self.closed
        self.rows.extend(dict(row) for row in rows)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def recording_factory():
    created: list[RecordingSink] = []

    def factory(path, *, metadata):
        child = RecordingSink(path, metadata=metadata)
        created.append(child)
        return child

    return created, factory


# -- demultiplexing ---------------------------------------------------------


def test_routing_sink_satisfies_sink_protocol(tmp_path):
    assert isinstance(RoutingSink(tmp_path), Sink)


def test_multi_series_interleaved_rows_land_in_their_own_files(tmp_path):
    sink = RoutingSink(tmp_path, today=fixed_today)
    sink.write(
        [
            sample(series="packA", run="r1", t=0.0, v=4.0),
            sample(series="packB", run="r1", t=0.0, v=3.0),
            sample(series="packA", run="r1", t=1.0, v=3.9),
        ]
    )
    sink.write(
        [
            sample(series="packB", run="r1", t=1.0, v=2.9),
            sample(series="packA", run="r1", t=2.0, v=3.8),
        ]
    )
    sink.close()

    file_a = tmp_path / "LOCAL__packA__20260719_001.bdf.csv"
    file_b = tmp_path / "LOCAL__packB__20260719_001.bdf.csv"
    assert sorted(p.name for p in tmp_path.glob("*.bdf.csv")) == [file_a.name, file_b.name]
    assert [(r["test_time_second"], r["voltage_volt"]) for r in read_rows(file_a)] == [
        ("0.0", "4.0"),
        ("1.0", "3.9"),
        ("2.0", "3.8"),
    ]
    assert [(r["test_time_second"], r["voltage_volt"]) for r in read_rows(file_b)] == [
        ("0.0", "3.0"),
        ("1.0", "2.9"),
    ]


def test_default_stream_for_routing_free_samples(tmp_path):
    sink = RoutingSink(tmp_path, today=fixed_today)
    sink.write([sample(t=0.0), sample(t=1.0)])
    sink.close()

    path = tmp_path / "LOCAL__default__20260719_001.bdf.csv"
    assert [p.name for p in tmp_path.glob("*.bdf.csv")] == [path.name]
    assert len(read_rows(path)) == 2
    sidecar = read_sidecar(path)
    assert sidecar["metadata"]["series_id"] is None
    assert sidecar["metadata"]["run_id"] is None
    assert sidecar["metadata"]["segment"] == 1
    assert sidecar["finalized"] is True


def test_routed_and_routing_free_samples_mix_in_one_batch(tmp_path):
    sink = RoutingSink(tmp_path, today=fixed_today)
    sink.write([sample(series="packA", run="r1"), sample()])
    sink.close()

    names = sorted(p.name for p in tmp_path.glob("*.bdf.csv"))
    assert names == [
        "LOCAL__default__20260719_001.bdf.csv",
        "LOCAL__packA__20260719_001.bdf.csv",
    ]


def test_non_string_series_id_is_coerced_deterministically(tmp_path, recording_factory):
    created, factory = recording_factory
    sink = RoutingSink(tmp_path, sink_factory=factory, today=fixed_today)
    sink.write([sample(series=7, run=1)])
    sink.close()
    assert created[0].path.name == "LOCAL__7__20260719_001.bdf.csv"
    assert created[0].metadata["series_id"] == "7"
    assert created[0].metadata["run_id"] == "1"


def test_reserved_keys_never_reach_any_csv(tmp_path):
    sink = RoutingSink(tmp_path, today=fixed_today)
    sink.write(
        [
            sample(series="packA", run="r9", t=0.0),
            sample(series="packB", run="r9", t=0.0),
            sample(t=0.0),
        ]
    )
    sink.close()

    files = list(tmp_path.glob("*.bdf.csv"))
    assert len(files) == 3
    for path in files:
        body = path.read_text(encoding="utf-8")
        for forbidden in ("series_id", "run_id", "packA", "packB", "r9"):
            assert forbidden not in body, f"{forbidden!r} leaked into {path.name}"
        assert body.splitlines()[0] == "test_time_second,voltage_volt,current_ampere"


def test_empty_batch_writes_nothing(tmp_path):
    sink = RoutingSink(tmp_path, today=fixed_today)
    sink.write([])
    assert list(tmp_path.glob("*")) == []
    sink.close()
    assert list(tmp_path.glob("*")) == []


# -- run semantics and rotation ---------------------------------------------


def test_run_id_change_closes_segment_and_opens_next_file(tmp_path):
    sink = RoutingSink(tmp_path, today=fixed_today)
    sink.write([sample(series="pack", run="flight-1", t=0.0)])
    sink.write([sample(series="pack", run="flight-1", t=1.0)])
    sink.write([sample(series="pack", run="flight-2", t=0.0)])

    first = tmp_path / "LOCAL__pack__20260719_001.bdf.csv"
    second = tmp_path / "LOCAL__pack__20260719_002.bdf.csv"
    # The run change finalized the first segment before close() was ever called.
    first_sidecar = read_sidecar(first)
    assert first_sidecar["finalized"] is True
    assert first_sidecar["rows"] == 2
    assert first_sidecar["metadata"]["run_id"] == "flight-1"
    assert first_sidecar["metadata"]["segment"] == 1
    sink.close()
    second_sidecar = read_sidecar(second)
    assert second_sidecar["metadata"]["run_id"] == "flight-2"
    assert second_sidecar["metadata"]["segment"] == 2
    assert len(read_rows(second)) == 1


def test_returning_run_id_opens_a_new_segment_not_the_old_file(tmp_path):
    # r1 after r2 must NOT merge back into the first file: that would create a
    # non-monotonic test_time_second, which is invalid BDF.
    sink = RoutingSink(tmp_path, today=fixed_today)
    sink.write(
        [
            sample(series="pack", run="r1", t=0.0),
            sample(series="pack", run="r2", t=0.0),
            sample(series="pack", run="r1", t=0.0),
        ]
    )
    sink.close()

    files = sorted(p.name for p in tmp_path.glob("*.bdf.csv"))
    assert files == [
        "LOCAL__pack__20260719_001.bdf.csv",
        "LOCAL__pack__20260719_002.bdf.csv",
        "LOCAL__pack__20260719_003.bdf.csv",
    ]
    assert all(len(read_rows(tmp_path / name)) == 1 for name in files)


def test_row_rotation_boundaries_split_batches_exactly(tmp_path):
    sink = RoutingSink(tmp_path, rotate_after_rows=3, today=fixed_today)
    sink.write([sample(series="s", run="r", t=float(n)) for n in range(5)])
    sink.write([sample(series="s", run="r", t=float(n)) for n in range(5, 9)])

    names = [f"LOCAL__s__20260719_{seq:03d}.bdf.csv" for seq in (1, 2, 3)]
    assert sorted(p.name for p in tmp_path.glob("*.bdf.csv")) == names
    times = [[r["test_time_second"] for r in read_rows(tmp_path / n)] for n in names]
    assert times == [["0.0", "1.0", "2.0"], ["3.0", "4.0", "5.0"], ["6.0", "7.0", "8.0"]]
    # Full segments are finalized eagerly, before close().
    for name, expected_segment in zip(names, (1, 2, 3)):
        sidecar = read_sidecar(tmp_path / name)
        assert sidecar["finalized"] is True
        assert sidecar["metadata"]["segment"] == expected_segment
        assert sidecar["metadata"]["run_id"] == "r"  # rotation without a run change
    sink.close()


def test_time_rotation_uses_injected_clock(tmp_path, fake_clock):
    sink = RoutingSink(tmp_path, rotate_after_s=10.0, clock=fake_clock, today=fixed_today)
    sink.write([sample(series="s", t=0.0)])
    fake_clock.now = 5.0
    sink.write([sample(series="s", t=5.0)])  # under the limit: same file
    fake_clock.now = 10.0
    sink.write([sample(series="s", t=0.0)])  # at the limit: rotates
    sink.close()

    first = tmp_path / "LOCAL__s__20260719_001.bdf.csv"
    second = tmp_path / "LOCAL__s__20260719_002.bdf.csv"
    assert len(read_rows(first)) == 2
    assert len(read_rows(second)) == 1
    assert read_sidecar(first)["metadata"]["segment"] == 1
    assert read_sidecar(second)["metadata"]["segment"] == 2


def test_time_and_row_rotation_whichever_trips_first(tmp_path, fake_clock):
    sink = RoutingSink(
        tmp_path, rotate_after_s=10.0, rotate_after_rows=2, clock=fake_clock, today=fixed_today
    )
    sink.write([sample(series="s", t=0.0), sample(series="s", t=1.0)])  # row limit trips
    fake_clock.now = 1.0
    sink.write([sample(series="s", t=0.0)])  # opens segment 2 at t=1
    fake_clock.now = 12.0
    sink.write([sample(series="s", t=0.0)])  # 11 s elapsed: time limit trips
    sink.close()

    rows_per_file = [
        len(read_rows(tmp_path / f"LOCAL__s__20260719_{seq:03d}.bdf.csv")) for seq in (1, 2, 3)
    ]
    assert rows_per_file == [2, 1, 1]


# -- sanitization and naming ------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PACK-001", "PACK-001"),  # already clean
        ("a__b", "a-b"),  # "__" is the BDF filename separator
        ("a____b", "a-b"),
        ("ser/ial\\no", "ser-ial-no"),  # path separators
        ('tricky<>:"|?*id', "tricky-id"),  # Windows-illegal characters
        ("ctrl\x01char", "ctrl-char"),  # control characters
        ("  spaced  name  ", "spaced-name"),
        ("_x_", "x"),  # edge "_" would recreate "__" at the separators
        ("", "unknown"),
        ("   ", "unknown"),
        ("...", "unknown"),
        ("__", "unknown"),
        ("x" * 100, "x" * 60),  # length cap
        ("y" * 59 + "_z", "y" * 59),  # truncation cannot leave a trailing "_"
    ],
)
def test_sanitize_cell_name(raw, expected):
    assert sanitize_cell_name(raw) == expected


def test_collision_after_sanitization_is_disambiguated_deterministically(tmp_path):
    def build_and_write(directory) -> list[str]:
        sink = RoutingSink(directory, sink_factory=RecordingSink, today=fixed_today)
        sink.write([sample(series="pack/1"), sample(series="pack?1")])
        sink.close()
        return [p.name for files in sink.files_by_series.values() for p in files]

    suffix = hashlib.sha256(b"pack?1").hexdigest()[:8]
    expected = [
        "LOCAL__pack-1__20260719_001.bdf.csv",
        f"LOCAL__pack-1-{suffix}__20260719_001.bdf.csv",
    ]
    assert build_and_write(tmp_path / "out1") == expected
    # A fresh sink (same arrival order, clean directory) allocates the same names.
    assert build_and_write(tmp_path / "out2") == expected


def test_series_named_default_cannot_collide_with_default_stream(tmp_path, recording_factory):
    created, factory = recording_factory
    sink = RoutingSink(tmp_path, sink_factory=factory, today=fixed_today)
    sink.write([sample(series="default"), sample()])
    sink.close()

    suffix = hashlib.sha256(b"default").hexdigest()[:8]
    assert created[0].path.name == f"LOCAL__default-{suffix}__20260719_001.bdf.csv"
    assert created[1].path.name == "LOCAL__default__20260719_001.bdf.csv"


def test_existing_files_on_disk_are_not_overwritten(tmp_path):
    stale = tmp_path / "LOCAL__pack__20260719_001.bdf.csv"
    stale.write_text("pre-existing\n", encoding="utf-8")
    sink = RoutingSink(tmp_path, today=fixed_today)
    sink.write([sample(series="pack")])
    sink.close()

    assert stale.read_text(encoding="utf-8") == "pre-existing\n"
    assert (tmp_path / "LOCAL__pack__20260719_002.bdf.csv").exists()


def test_concurrent_sinks_never_claim_the_same_path(tmp_path):
    """Path allocation is an atomic on-disk reservation, not check-then-claim.

    Two independent collections into one directory, where the first child sink
    has not yet written a single byte when the second allocates (the TOCTOU
    window a bare exists() check leaves open): the O_EXCL reservation made at
    allocation time must keep their sequence numbers apart.
    """
    allocated: list[Path] = []

    class BufferingSink:
        """Child sink that holds everything in memory -- nothing hits its file."""

        def __init__(self, path, *, metadata):
            self.path = Path(path)
            allocated.append(self.path)

        def write(self, rows) -> None:
            pass

        def close(self) -> None:
            pass

    first = RoutingSink(tmp_path, sink_factory=BufferingSink, today=fixed_today)
    second = RoutingSink(tmp_path, sink_factory=BufferingSink, today=fixed_today)
    first.write([sample(series="pack")])  # claims _001, writes no data yet
    second.write([sample(series="pack")])  # must observe the claim, take _002
    first.close()
    second.close()

    assert [p.name for p in allocated] == [
        "LOCAL__pack__20260719_001.bdf.csv",
        "LOCAL__pack__20260719_002.bdf.csv",
    ]
    assert allocated[0] != allocated[1]


def test_case_insensitive_collision_gets_hash_suffix(tmp_path, recording_factory):
    # "CellA" and "cella" are one file on NTFS/APFS: the later arrival must be
    # disambiguated exactly like an exact-name collision.
    created, factory = recording_factory
    sink = RoutingSink(tmp_path, sink_factory=factory, today=fixed_today)
    sink.write([sample(series="CellA"), sample(series="cella")])
    sink.close()

    suffix = hashlib.sha256(b"cella").hexdigest()[:8]
    assert created[0].path.name == "LOCAL__CellA__20260719_001.bdf.csv"
    assert created[1].path.name == f"LOCAL__cella-{suffix}__20260719_001.bdf.csv"


def test_case_colliding_series_do_not_entangle_sequence_numbers(tmp_path, recording_factory):
    created, factory = recording_factory
    sink = RoutingSink(tmp_path, sink_factory=factory, rotate_after_rows=1, today=fixed_today)
    sink.write([sample(series="CellA"), sample(series="cella")])
    sink.write([sample(series="CellA"), sample(series="cella")])
    sink.close()

    suffix = hashlib.sha256(b"cella").hexdigest()[:8]
    assert [c.path.name for c in created] == [
        "LOCAL__CellA__20260719_001.bdf.csv",
        f"LOCAL__cella-{suffix}__20260719_001.bdf.csv",
        "LOCAL__CellA__20260719_002.bdf.csv",
        f"LOCAL__cella-{suffix}__20260719_002.bdf.csv",
    ]


# -- series_info hook and per-segment metadata ------------------------------


def test_series_info_names_files_and_merges_sidecar_metadata(tmp_path):
    def series_info(series_id: str):
        return f"Bay {series_id}", {"bay": series_id, "chemistry": "LFP"}

    sink = RoutingSink(
        tmp_path,
        institution="SINTEF",
        series_info=series_info,
        metadata={"site": "lab-3"},
        today=fixed_today,
    )
    sink.write([sample(series="A", run="cycle-1")])
    sink.close()

    path = tmp_path / "SINTEF__Bay-A__20260719_001.bdf.csv"  # hook name is sanitized too
    assert path.exists()
    assert read_sidecar(path)["metadata"] == {
        "site": "lab-3",
        "bay": "A",
        "chemistry": "LFP",
        "series_id": "A",
        "run_id": "cycle-1",
        "segment": 1,
    }


def test_mid_stream_series_gets_its_own_file_and_early_sidecar(tmp_path):
    sink = RoutingSink(tmp_path, today=fixed_today)
    sink.write([sample(series="early", run="r1", t=0.0)])
    sink.write([sample(series="early", run="r1", t=1.0), sample(series="late", run="r1", t=0.0)])

    late = tmp_path / "LOCAL__late__20260719_001.bdf.csv"
    assert read_rows(late) == [
        {"test_time_second": "0.0", "voltage_volt": "3.7", "current_ampere": "0.0"}
    ]
    # A crash right now must leave the newcomer's sidecar on disk, unfinalised.
    sidecar = read_sidecar(late)
    assert sidecar["finalized"] is False
    assert sidecar["metadata"]["series_id"] == "late"
    assert sidecar["metadata"]["segment"] == 1
    sink.close()
    assert read_sidecar(late)["finalized"] is True


# -- close semantics and reporting ------------------------------------------


def test_close_finalizes_all_children_and_is_idempotent(tmp_path):
    sink = RoutingSink(tmp_path, today=fixed_today)
    sink.write([sample(series="a"), sample(series="b"), sample()])
    sink.close()
    sink.close()  # idempotent

    for path in tmp_path.glob("*.bdf.csv"):
        assert read_sidecar(path)["finalized"] is True
    with pytest.raises(ValueError, match="closed"):
        sink.write([sample()])


def test_close_without_writes_creates_no_files(tmp_path):
    sink = RoutingSink(tmp_path, today=fixed_today)
    sink.close()
    assert list(tmp_path.glob("*")) == []


def test_files_by_series_reports_every_segment_in_order(tmp_path):
    sink = RoutingSink(tmp_path, rotate_after_rows=2, today=fixed_today)
    sink.write([sample(series="a", run="r1", t=float(n)) for n in range(3)])
    sink.write([sample(series="a", run="r2", t=0.0), sample(t=0.0)])
    sink.close()

    files = sink.files_by_series
    assert [p.name for p in files["a"]] == [
        "LOCAL__a__20260719_001.bdf.csv",
        "LOCAL__a__20260719_002.bdf.csv",
        "LOCAL__a__20260719_003.bdf.csv",
    ]
    assert [p.name for p in files[None]] == ["LOCAL__default__20260719_001.bdf.csv"]
    # The property returns a copy, not live state.
    files["a"].clear()
    assert len(sink.files_by_series["a"]) == 3


def test_sink_factory_is_injectable_and_reservations_stay_empty(tmp_path, recording_factory):
    created, factory = recording_factory
    sink = RoutingSink(tmp_path / "out", sink_factory=factory, today=fixed_today)
    sink.write([sample(series="a", run="r1"), sample(series="b", run="r1")])
    sink.close()

    assert [c.path.name for c in created] == [
        "LOCAL__a__20260719_001.bdf.csv",
        "LOCAL__b__20260719_001.bdf.csv",
    ]
    assert all(c.closed for c in created)
    # Path allocation reserves each claimed path on disk (TOCTOU guard); with
    # an in-memory child sink the reservations stay empty files.
    for child in created:
        assert child.path.exists()
        assert child.path.read_text(encoding="utf-8") == ""
    # Reserved keys were stripped before delegation (defense in depth).
    assert all("series_id" not in row and "run_id" not in row for c in created for row in c.rows)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"institution": ""},
        {"institution": "A__B"},
        {"rotate_after_s": 0},
        {"rotate_after_s": -1.0},
        {"rotate_after_rows": 0},
    ],
)
def test_constructor_rejects_bad_arguments(tmp_path, kwargs):
    with pytest.raises(ValueError):
        RoutingSink(tmp_path, **kwargs)


# -- invariant I5: the source owns the per-(series, run) timebase ------------


def test_i5_source_supplied_timebase_survives_harvester_stamping(tmp_path, fake_clock):
    """A series appearing mid-run starts its file at t=0, not at elapsed time.

    The harvester stamps its shared elapsed-collection time only onto rows
    that lack test_time_second; a routing source supplies its own zero-based
    per-(series, run) clock, and RoutingSink must pass it through untouched.
    """

    class TwoPackSource:
        name = "twopack"

        def __init__(self) -> None:
            self.polls = 0

        def metadata(self):
            return {"kind": "fake"}

        def poll(self):
            self.polls += 1
            if self.polls == 1:
                return [sample(series="A", run="r1", t=0.0, v=4.0)]
            if self.polls == 2:
                # Pack B comes online one second into the collection and
                # supplies its own zero-based timebase (invariant I5).
                return [
                    sample(series="A", run="r1", t=1.0, v=3.9),
                    sample(series="B", run="r1", t=0.0, v=3.0),
                ]
            return []

    harvester = Harvester()
    harvester.register(TwoPackSource())
    sink = RoutingSink(tmp_path, clock=fake_clock, today=fixed_today)
    harvester.collect(
        "twopack",
        duration_s=3.0,
        interval_s=1.0,
        sink=sink,
        clock=fake_clock,
        sleep=fake_clock.sleep,
    )
    sink.close()

    times_a = [
        float(r["test_time_second"]) for r in read_rows(tmp_path / "LOCAL__A__20260719_001.bdf.csv")
    ]
    times_b = [
        float(r["test_time_second"]) for r in read_rows(tmp_path / "LOCAL__B__20260719_001.bdf.csv")
    ]
    assert times_a == [0.0, 1.0]
    # B appeared when the harvester's shared elapsed time was 1.0 s; its file
    # must nevertheless start at 0.0 -- the source-supplied stamp wins.
    assert times_b == [0.0]


class _FixedSource:
    """Source returning the same batch on every poll (rows copied per poll)."""

    def __init__(self, name: str, batch: list[dict]) -> None:
        self.name = name
        self._batch = batch

    def metadata(self):
        return {"kind": "fake"}

    def poll(self):
        return [dict(row) for row in self._batch]


def _collect(source, fake_clock, sink) -> None:
    harvester = Harvester()
    harvester.register(source)
    harvester.collect(
        source.name,
        duration_s=3.0,
        interval_s=1.0,
        sink=sink,
        clock=fake_clock,
        sleep=fake_clock.sleep,
    )


def _i5_warnings(caplog) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == "battfeed.harvester"
        and record.levelno == logging.WARNING
        and "invariant I5" in record.getMessage()
    ]


def test_harvester_warns_once_when_stamping_a_routed_row(fake_clock, list_sink, caplog):
    # Three polls, each with routing-key rows missing test_time_second: the
    # contract violation is reported exactly once per source per collect run.
    source = _FixedSource(
        "bad-routing",
        [
            {"series_id": "A", "run_id": "r1", "voltage_volt": 3.7, "current_ampere": 0.0},
            {"series_id": "B", "voltage_volt": 3.6, "current_ampere": 0.0},
        ],
    )
    with caplog.at_level(logging.WARNING, logger="battfeed.harvester"):
        _collect(source, fake_clock, list_sink)

    warnings = _i5_warnings(caplog)
    assert len(warnings) == 1
    assert "'bad-routing'" in warnings[0].getMessage()
    assert "series_id/run_id" in warnings[0].getMessage()


def test_harvester_does_not_warn_for_routing_source_with_own_timebase(
    fake_clock, list_sink, caplog
):
    source = _FixedSource(
        "good-routing",
        [{"series_id": "A", "test_time_second": 0.0, "voltage_volt": 3.7, "current_ampere": 0.0}],
    )
    with caplog.at_level(logging.WARNING, logger="battfeed.harvester"):
        _collect(source, fake_clock, list_sink)
    assert _i5_warnings(caplog) == []


def test_harvester_does_not_warn_for_stamped_non_routing_source(fake_clock, list_sink, caplog):
    # The stamp is exactly right for a single-object source: no warning.
    source = _FixedSource("plain", [{"voltage_volt": 3.7, "current_ampere": 0.0}])
    with caplog.at_level(logging.WARNING, logger="battfeed.harvester"):
        _collect(source, fake_clock, list_sink)
    assert _i5_warnings(caplog) == []
