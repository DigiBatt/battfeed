from __future__ import annotations

import datetime
import json
import sys

import pytest

from battfeed import BdfCsvSink, __version__
from battfeed.sinks.bdf_csv import dataset_filename, validate_file


def test_header_leads_with_required_trio_then_sorted_extras(tmp_path):
    path = tmp_path / "TEST__Cell__20260707_001.bdf.csv"
    sink = BdfCsvSink(path)
    sink.write(
        [
            {
                "surface_temperature_celsius": 25.0,
                "voltage_volt": 3.0,
                "test_time_second": 0.0,
                "current_ampere": -0.002,
                "ambient_temperature_celsius": 24.0,
            },
            {"test_time_second": 1.0, "voltage_volt": 2.99},  # missing columns -> empty cells
        ]
    )
    sink.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == (
        "test_time_second,voltage_volt,current_ampere,"
        "ambient_temperature_celsius,surface_temperature_celsius"
    )
    assert lines[1] == "0.0,3.0,-0.002,24.0,25.0"
    assert lines[2] == "1.0,2.99,,,"


def test_sidecar_written_on_close(tmp_path):
    path = tmp_path / "TEST__Cell__20260707_002.bdf.csv"
    sink = BdfCsvSink(path, metadata={"operator": "demo", "institution": "TEST"})
    sink.write([{"test_time_second": 0.0, "voltage_volt": 3.0, "current_ampere": 0.0}])
    sink.close()

    sidecar = tmp_path / "TEST__Cell__20260707_002.meta.json"
    assert sidecar.exists()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["metadata"] == {"operator": "demo", "institution": "TEST"}
    assert payload["battfeed_version"] == __version__
    assert payload["rows"] == 1
    assert payload["columns"][:3] == ["test_time_second", "voltage_volt", "current_ampere"]
    datetime.datetime.fromisoformat(payload["started_at"])
    datetime.datetime.fromisoformat(payload["finished_at"])


def test_close_without_writes_still_produces_conforming_file(tmp_path):
    path = tmp_path / "TEST__Cell__20260707_003.bdf.csv"
    sink = BdfCsvSink(path)
    sink.close()
    sink.close()  # idempotent

    assert path.read_text(encoding="utf-8").splitlines() == [
        "test_time_second,voltage_volt,current_ampere"
    ]
    assert (tmp_path / "TEST__Cell__20260707_003.meta.json").exists()
    with pytest.raises(ValueError, match="closed"):
        sink.write([{"test_time_second": 0.0}])


def test_reserved_routing_keys_never_become_columns_or_rows(tmp_path):
    path = tmp_path / "TEST__Cell__20260707_010.bdf.csv"
    sink = BdfCsvSink(path)
    sink.write(
        [
            {
                "series_id": "pack-A",
                "run_id": "flight-7",
                "test_time_second": 0.0,
                "voltage_volt": 3.7,
                "current_ampere": -0.5,
            },
            {"series_id": "pack-A", "test_time_second": 1.0, "voltage_volt": 3.6},
        ]
    )
    sink.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "test_time_second,voltage_volt,current_ampere"
    assert lines[1] == "0.0,3.7,-0.5"
    assert lines[2] == "1.0,3.6,"
    body = path.read_text(encoding="utf-8")
    for key in ("series_id", "run_id", "pack-A", "flight-7"):
        assert key not in body


def test_reserved_keys_stripped_from_explicit_columns(tmp_path):
    path = tmp_path / "TEST__Cell__20260707_011.bdf.csv"
    sink = BdfCsvSink(path, columns=["series_id", "voltage_volt", "current_ampere"])
    sink.write([{"series_id": "x", "voltage_volt": 3.0, "current_ampere": 0.0}])
    sink.close()

    header = path.read_text(encoding="utf-8").splitlines()[0]
    assert header == "test_time_second,voltage_volt,current_ampere"


def test_sidecar_written_early_and_finalised_on_close(tmp_path):
    path = tmp_path / "TEST__Cell__20260707_012.bdf.csv"
    sidecar = tmp_path / "TEST__Cell__20260707_012.meta.json"
    sink = BdfCsvSink(path)
    sink.write([{"test_time_second": 0.0, "voltage_volt": 3.0, "current_ampere": 0.0}])

    # A crash here must leave a valid, unfinalised sidecar on disk.
    assert sidecar.exists()
    early = json.loads(sidecar.read_text(encoding="utf-8"))
    assert early["finalized"] is False
    assert early["finished_at"] is None
    assert early["columns"][:3] == ["test_time_second", "voltage_volt", "current_ampere"]

    sink.write([{"test_time_second": 1.0, "voltage_volt": 2.9, "current_ampere": 0.0}])
    sink.close()

    final = json.loads(sidecar.read_text(encoding="utf-8"))
    assert final["finalized"] is True
    assert final["rows"] == 2
    datetime.datetime.fromisoformat(final["finished_at"])


def test_sidecar_rewritten_periodically_mid_collection(tmp_path):
    path = tmp_path / "TEST__Cell__20260707_013.bdf.csv"
    sidecar = tmp_path / "TEST__Cell__20260707_013.meta.json"
    now = [1000.0]
    sink = BdfCsvSink(path, clock=lambda: now[0])

    # The early sidecar is written at open time with a row count of 0, and is
    # not refreshed by further writes within the rewrite interval.
    sink.write([{"test_time_second": 0.0, "voltage_volt": 3.0, "current_ampere": 0.0}])
    assert json.loads(sidecar.read_text(encoding="utf-8"))["rows"] == 0
    now[0] += 5.0
    sink.write([{"test_time_second": 1.0, "voltage_volt": 2.9, "current_ampere": 0.0}])
    assert json.loads(sidecar.read_text(encoding="utf-8"))["rows"] == 0

    # Past the interval, the on-disk sidecar catches up to the current row count.
    now[0] += 60.0
    sink.write([{"test_time_second": 2.0, "voltage_volt": 2.8, "current_ampere": 0.0}])
    mid = json.loads(sidecar.read_text(encoding="utf-8"))
    assert mid["rows"] == 3
    assert mid["finalized"] is False
    sink.close()


def test_dataset_filename_follows_bdf_convention():
    name = dataset_filename("SINTEF", "CR2032-01", datetime.date(2026, 7, 7), 3)
    assert name == "SINTEF__CR2032-01__20260707_003.bdf.csv"

    with pytest.raises(ValueError, match="__"):
        dataset_filename("SINTEF", "bad__cell", datetime.date(2026, 7, 7), 1)
    with pytest.raises(ValueError, match="seq"):
        dataset_filename("SINTEF", "cell", datetime.date(2026, 7, 7), 1000)


def test_validate_file_explains_missing_bdf_extra(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "bdf", None)  # force `import bdf` to fail
    with pytest.raises(ImportError, match=r"battfeed\[bdf\]"):
        validate_file(tmp_path / "whatever.bdf.csv")
