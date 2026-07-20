"""ParquetSink tests (require the pyarrow optional extra, except the ImportError one)."""

from __future__ import annotations

import datetime
import json
import logging
import sys
from pathlib import Path

import pytest

from battfeed import ParquetSink, __version__

pyarrow = pytest.importorskip("pyarrow", reason="ParquetSink tests need battfeed[parquet]")
import pyarrow.parquet as pq  # noqa: E402  (guarded by the importorskip above)


def test_round_trip_via_pyarrow(tmp_path):
    path = tmp_path / "capture.parquet"
    sink = ParquetSink(path)
    sink.write(
        [
            {"test_time_second": 0.0, "voltage_volt": 3.71, "current_ampere": -0.002},
            {"test_time_second": 1.0, "voltage_volt": 3.70, "current_ampere": -0.002},
        ]
    )
    sink.close()

    table = pq.read_table(path)
    assert table.column_names == ["test_time_second", "voltage_volt", "current_ampere"]
    assert table.num_rows == 2
    assert table.column("voltage_volt").to_pylist() == [3.71, 3.70]
    assert table.column("test_time_second").to_pylist() == [0.0, 1.0]


def test_ragged_rows_null_fill_and_first_seen_column_order(tmp_path):
    path = tmp_path / "ragged.parquet"
    sink = ParquetSink(path)
    sink.write([{"voltage_volt": 3.7, "current_ampere": -0.5}])
    sink.write([{"current_ampere": -0.4, "surface_temperature_celsius": 25.0}])
    sink.close()

    table = pq.read_table(path)
    # Union of keys in first-seen order; missing values become nulls.
    assert table.column_names == ["voltage_volt", "current_ampere", "surface_temperature_celsius"]
    assert table.column("voltage_volt").to_pylist() == [3.7, None]
    assert table.column("current_ampere").to_pylist() == [-0.5, -0.4]
    assert table.column("surface_temperature_celsius").to_pylist() == [None, 25.0]


def test_sidecar_contents(tmp_path):
    path = tmp_path / "capture.parquet"
    sink = ParquetSink(path, metadata={"operator": "demo", "institution": "TEST"})
    sink.write([{"test_time_second": 0.0, "voltage_volt": 3.0, "current_ampere": 0.0}])
    sink.close()

    sidecar = tmp_path / "capture.meta.json"
    assert sidecar.exists()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["file"] == "capture.parquet"
    assert payload["metadata"] == {"operator": "demo", "institution": "TEST"}
    assert payload["battfeed_version"] == __version__
    assert payload["columns"] == ["test_time_second", "voltage_volt", "current_ampere"]
    assert payload["rows"] == 1
    assert payload["finalized"] is True
    datetime.datetime.fromisoformat(payload["started_at"])
    datetime.datetime.fromisoformat(payload["finished_at"])


def test_reserved_routing_keys_kept_as_ordinary_columns(tmp_path):
    # Deliberate contrast with BdfCsvSink: parquet is an analytics format, not
    # BDF, so series_id/run_id survive as columns for downstream group-bys.
    path = tmp_path / "routed.parquet"
    sink = ParquetSink(path)
    sink.write(
        [
            {"series_id": "pack-A", "run_id": "flight-7", "voltage_volt": 3.7},
            {"series_id": "pack-B", "run_id": "flight-1", "voltage_volt": 3.9},
        ]
    )
    sink.close()

    table = pq.read_table(path)
    assert table.column("series_id").to_pylist() == ["pack-A", "pack-B"]
    assert table.column("run_id").to_pylist() == ["flight-7", "flight-1"]


def test_import_error_names_the_parquet_extra(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pyarrow", None)  # force `import pyarrow` to fail
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)
    with pytest.raises(ImportError, match=r"battfeed\[parquet\]"):
        ParquetSink(tmp_path / "capture.parquet")


def test_close_is_idempotent_and_write_after_close_raises(tmp_path):
    path = tmp_path / "empty.parquet"
    sink = ParquetSink(path)
    sink.close()
    sink.close()  # idempotent: file written once, no error

    table = pq.read_table(path)
    assert table.num_rows == 0
    payload = json.loads((tmp_path / "empty.meta.json").read_text(encoding="utf-8"))
    assert payload["rows"] == 0
    assert payload["finalized"] is True

    with pytest.raises(ValueError, match="closed"):
        sink.write([{"voltage_volt": 3.7}])


def _strict_loads(text: str) -> dict:
    """Parse one JSON object, rejecting the non-RFC NaN/Infinity tokens."""

    def _reject(token: str) -> None:
        raise AssertionError(f"non-RFC 8259 token {token!r} in rescue file")

    return json.loads(text, parse_constant=_reject)


@pytest.mark.parametrize(
    "rows",
    [
        [{"a": 1}, {"a": "x"}],
        [{"a": 1.5}, {"a": "x"}],
        [{"a": True}, {"a": None}, {"a": 2}],
    ],
    ids=["int_then_str", "float_then_str", "bool_none_int"],
)
def test_mixed_type_columns_rescue_every_row_instead_of_raising(tmp_path, rows, caplog):
    path = tmp_path / "mix.parquet"
    sink = ParquetSink(path, metadata={"operator": "demo"})
    sink.write(rows)
    with caplog.at_level(logging.ERROR, logger="battfeed.sinks.parquet"):
        sink.close()  # ArrowInvalid must not escape

    assert not path.exists()  # no (partial) parquet file at the final path
    rescue = tmp_path / "mix.rescue.ndjson"
    assert rescue.exists()
    lines = rescue.read_text(encoding="utf-8").splitlines()
    assert [_strict_loads(line) for line in lines] == rows  # every buffered row rescued

    sidecar = json.loads((tmp_path / "mix.meta.json").read_text(encoding="utf-8"))
    assert sidecar["finalized"] is False
    assert "error" in sidecar and sidecar["error"]  # names the failure
    assert sidecar["rows"] == len(rows)
    assert str(rescue) in caplog.text


def test_rescue_file_is_rfc_valid_with_nonfinite_floats(tmp_path):
    path = tmp_path / "nan.parquet"
    sink = ParquetSink(path)
    sink.write([{"a": float("nan")}, {"a": "x"}])  # mixed types force the rescue path
    sink.close()

    rescue = tmp_path / "nan.rescue.ndjson"
    payloads = [
        _strict_loads(line) for line in rescue.read_text(encoding="utf-8").splitlines() if line
    ]
    assert payloads == [{"a": None}, {"a": "x"}]  # NaN became null, not a poison token


def test_no_partial_file_at_final_path_on_write_failure(tmp_path):
    path = tmp_path / "boom.parquet"
    sink = ParquetSink(path)
    sink.write([{"voltage_volt": 3.7, "current_ampere": -0.5}])

    class BoomParquetModule:
        @staticmethod
        def write_table(table, dest):  # simulates a mid-write crash leaving partial bytes
            Path(dest).write_bytes(b"partial garbage")
            raise RuntimeError("disk exploded")

    sink._pq = BoomParquetModule
    sink.close()  # must not raise

    assert not path.exists()  # tmp + os.replace: nothing partial at the final path
    assert list(tmp_path.glob("*.tmp")) == []  # and the tmp file was cleaned up
    rescue = tmp_path / "boom.rescue.ndjson"
    lines = rescue.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [{"voltage_volt": 3.7, "current_ampere": -0.5}]
    sidecar = json.loads((tmp_path / "boom.meta.json").read_text(encoding="utf-8"))
    assert sidecar["finalized"] is False
    assert "RuntimeError" in sidecar["error"]
