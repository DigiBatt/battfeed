from __future__ import annotations

import pytest

from gleaned.sources.csvtail import CsvTailSource

COLUMN_MAP = {
    "Time/s": "test_time_second",
    "Ewe/mV": "voltage_volt",
    "I/mA": "current_ampere",
}
UNIT_SCALE = {"voltage_volt": 0.001, "current_ampere": 0.001}


def test_picks_up_appended_rows_with_mapping_and_scaling(tmp_path):
    log = tmp_path / "instrument.csv"
    source = CsvTailSource(log, COLUMN_MAP, unit_scale=UNIT_SCALE)

    assert source.poll() == []  # file does not exist yet

    log.write_text("Time/s,Ewe/mV,I/mA,Ns\n0.0,3200,-2.0,1\n1.0,3199,-2.0,1\n", encoding="utf-8")
    first = source.poll()
    assert first == [
        {
            "test_time_second": pytest.approx(0.0),
            "voltage_volt": pytest.approx(3.2),
            "current_ampere": pytest.approx(-0.002),
        },
        {
            "test_time_second": pytest.approx(1.0),
            "voltage_volt": pytest.approx(3.199),
            "current_ampere": pytest.approx(-0.002),
        },
    ]
    # Unmapped source columns ("Ns") are dropped by design.

    with open(log, "a", encoding="utf-8") as handle:
        handle.write("2.0,3198,-2.0,1\n")
    second = source.poll()
    assert len(second) == 1
    assert second[0]["voltage_volt"] == pytest.approx(3.198)

    assert source.poll() == []  # nothing new


def test_partial_trailing_line_is_deferred_until_complete(tmp_path):
    log = tmp_path / "instrument.csv"
    log.write_text("Time/s,Ewe/mV,I/mA\n", encoding="utf-8")
    source = CsvTailSource(log, COLUMN_MAP, unit_scale=UNIT_SCALE)
    assert source.poll() == []

    with open(log, "a", encoding="utf-8") as handle:
        handle.write("0.0,3200")  # writer got interrupted mid-row
    assert source.poll() == []

    with open(log, "a", encoding="utf-8") as handle:
        handle.write(",-2.0\n")
    assert source.poll() == [
        {"test_time_second": 0.0, "voltage_volt": 3.2, "current_ampere": -0.002}
    ]


def test_non_numeric_rows_are_skipped(tmp_path):
    log = tmp_path / "instrument.csv"
    log.write_text("Time/s,Ewe/mV,I/mA\n0.0,ERR,-2.0\n1.0,3200,-2.0\n", encoding="utf-8")
    source = CsvTailSource(log, COLUMN_MAP)
    rows = source.poll()
    assert len(rows) == 1
    assert rows[0]["test_time_second"] == 1.0
