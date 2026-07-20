"""Plausibility gate + BDF normalization: units, signs, filtering, routing keys.

Ported from the donor normalization tests; assertions adapted from the
proprietary canonical record to BDF sample rows (negated current, per-flight
zero-based test_time_second, series_id/run_id).
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from battfeed.sources.dji.gate import cell_column, normalize_flight

HEADER = [
    "CUSTOM.dateTime",
    "OSD.flyTime",
    "OSD.latitude",
    "OSD.longitude",
    "OSD.height",
    "OSD.isMotorOn",
    "OSD.droneType",
    "BATTERY.chargeLevel",
    "BATTERY.voltage",
    "BATTERY.current",
    "BATTERY.currentCapacity",
    "BATTERY.fullCapacity",
    "BATTERY.cellNum",
    "BATTERY.cellVoltage1",
    "BATTERY.cellVoltage2",
    "BATTERY.cellVoltage3",
    "BATTERY.temperature",
    "RECOVER.aircraftSerial",
    "RECOVER.batterySerial",
    "RECOVER.aircraftName",
]

GOOD_ROW = {
    "CUSTOM.dateTime": "2026-06-17T07:31:27.776Z",
    "OSD.flyTime": "5.0",
    "OSD.latitude": "63.4284",
    "OSD.longitude": "10.1530",
    "OSD.height": "12.3",
    "OSD.isMotorOn": "true",
    "OSD.droneType": "MavicAir2",
    "BATTERY.chargeLevel": "100",
    "BATTERY.voltage": "12.144",
    "BATTERY.current": "6.217",
    "BATTERY.currentCapacity": "3303",
    "BATTERY.fullCapacity": "3322",
    "BATTERY.cellNum": "3",
    "BATTERY.cellVoltage1": "4.048",
    "BATTERY.cellVoltage2": "4.063",
    "BATTERY.cellVoltage3": "4.051",
    "BATTERY.temperature": "21.5",
    "RECOVER.aircraftSerial": "3N3BH6M0020165",
    "RECOVER.batterySerial": "1Z3PH69EA104UV",
    "RECOVER.aircraftName": "dji aircraft",
}

# The corrupt-tail signature seen in real Mavic Air 2 logs.
CORRUPT_ROW = dict(
    GOOD_ROW,
    **{
        "CUSTOM.dateTime": "2026-06-17T07:43:53.269Z",
        "BATTERY.voltage": "51.586",
        "BATTERY.current": "1134578.0",
        "BATTERY.currentCapacity": "1873915279",
        "BATTERY.temperature": "3216.1",
        "BATTERY.cellVoltage1": "51.326",
        "BATTERY.cellVoltage2": "37.107",
        "BATTERY.cellVoltage3": "16.632",
    },
)

# Pre-GPS-lock rows carry the epoch as their timestamp.
EPOCH_ROW = dict(GOOD_ROW, **{"CUSTOM.dateTime": "1970-01-01T00:00:00Z"})

HASH = "abc123def456abc123def456"  # 24 hex; run_id uses the first 12


def write_csv(path: Path, rows: list[dict]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(rows)
    return path


@pytest.fixture
def flight_csv(tmp_path):
    later = dict(
        GOOD_ROW,
        **{
            "CUSTOM.dateTime": "2026-06-17T07:41:52.969Z",
            "OSD.flyTime": "630.3",
            "BATTERY.chargeLevel": "66",
            "BATTERY.voltage": "11.547",
            "BATTERY.current": "6.359",
            "BATTERY.currentCapacity": "2189",
            "BATTERY.temperature": "35.8",
            "BATTERY.cellVoltage1": "3.853",
            "BATTERY.cellVoltage2": "3.859",
            "BATTERY.cellVoltage3": "3.834",
        },
    )
    return write_csv(tmp_path / "flight.csv", [EPOCH_ROW, GOOD_ROW, later, CORRUPT_ROW])


def test_units_signs_and_mapping(flight_csv):
    fd = normalize_flight(flight_csv, HASH, source_file="raw.txt")
    assert len(fd.rows) == 2
    r = fd.rows[0]
    assert r["voltage_volt"] == 12.144
    assert r["current_ampere"] == -6.217  # DJI positive draw -> BDF discharge negative
    assert r["power_watt"] == pytest.approx(12.144 * -6.217, abs=1e-6)
    assert r["surface_temperature_celsius"] == 21.5
    assert r[cell_column(1)] == 4.048
    assert r[cell_column(2)] == 4.063
    assert r[cell_column(3)] == 4.051


def test_test_time_second_is_zero_based_per_flight(flight_csv):
    fd = normalize_flight(flight_csv, HASH)
    # First kept row is the GOOD_ROW at 07:31:27.776; the "later" row is
    # 07:41:52.969 -> 625.193 s after.
    assert fd.rows[0]["test_time_second"] == 0.0
    assert fd.rows[1]["test_time_second"] == pytest.approx(625.193, abs=1e-3)


def test_series_id_and_run_id(flight_csv):
    fd = normalize_flight(flight_csv, HASH)
    assert all(r["series_id"] == "3N3BH6M0020165:1Z3PH69EA104UV" for r in fd.rows)
    assert all(r["run_id"] == "flight-abc123def456" for r in fd.rows)
    assert fd.meta["series_id"] == "3N3BH6M0020165:1Z3PH69EA104UV"
    assert fd.meta["run_id"] == "flight-abc123def456"


def test_series_id_fallback_without_serials(tmp_path):
    row = dict(GOOD_ROW, **{"RECOVER.aircraftSerial": "", "RECOVER.batterySerial": ""})
    fd = normalize_flight(write_csv(tmp_path / "noserial.csv", [row]), HASH)
    # missing serial -> content-hash-derived id so distinct anonymous packs
    # never merge (HASH[:12] == "abc123def456").
    assert fd.rows[0]["series_id"] == "dji-drone:pack-abc123def456"


def test_drop_counting(flight_csv):
    fd = normalize_flight(flight_csv, HASH)
    assert fd.meta["rows_total"] == 4
    assert fd.meta["records"] == 2
    assert fd.meta["dropped"] == {"bad_ts": 1, "implausible": 1}
    assert fd.meta["soc_start_pct"] == 100.0
    assert fd.meta["soc_end_pct"] == 66.0


def test_cell_sum_inconsistency_is_dropped(tmp_path):
    # tail-corruption: every field individually plausible, but the pack voltage
    # disagrees with the reported cell voltages by volts.
    row = dict(
        GOOD_ROW,
        **{
            "CUSTOM.dateTime": "2026-06-17T07:32:00.000Z",
            "BATTERY.voltage": "13.119",
            "BATTERY.cellVoltage1": "3.765",
            "BATTERY.cellVoltage2": "3.774",
            "BATTERY.cellVoltage3": "3.739",
        },
    )
    fd = normalize_flight(write_csv(tmp_path / "tail.csv", [GOOD_ROW, row]), HASH)
    assert len(fd.rows) == 1
    assert fd.meta["dropped"]["implausible"] == 1


def test_extension_columns_declared(flight_csv):
    fd = normalize_flight(flight_csv, HASH)
    assert fd.extension_columns == [cell_column(1), cell_column(2), cell_column(3)]
    assert fd.meta["extension_columns"] == fd.extension_columns


def test_gps_carried_only_when_requested(flight_csv):
    off = normalize_flight(flight_csv, HASH, include_gps=False)
    assert "last_latitude" not in off.meta["flight_context"]
    on = normalize_flight(flight_csv, HASH, include_gps=True)
    assert on.meta["flight_context"]["last_latitude"] == pytest.approx(63.4284)


def test_garbage_gps_is_omitted(tmp_path):
    row = dict(GOOD_ROW, **{"OSD.latitude": "-5.09e+195", "OSD.longitude": "-2.15e+64"})
    fd = normalize_flight(write_csv(tmp_path / "gps.csv", [row]), HASH, include_gps=True)
    assert len(fd.rows) == 1
    assert "last_latitude" not in fd.meta["flight_context"]


def test_rows_carry_no_reserved_leak_into_measurements(flight_csv):
    fd = normalize_flight(flight_csv, HASH)
    for r in fd.rows:
        # routing keys present (for the RoutingSink) but every measurement is numeric
        assert isinstance(r["series_id"], str) and isinstance(r["run_id"], str)
        for key in ("test_time_second", "voltage_volt", "current_ampere", "power_watt"):
            assert isinstance(r[key], (int, float))
