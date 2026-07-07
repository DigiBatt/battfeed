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
