"""Parsing and unit-normalization tests for the Android ADB source."""

from __future__ import annotations

import pytest

from battfeed.sources.android.parser import (
    battery_health,
    battery_status,
    dumpsys_soc_pct,
    normalize_capacity_pct,
    normalize_current_a,
    normalize_temperature_c,
    normalize_voltage_v,
    parse_adb_devices,
    parse_dumpsys_battery,
    parse_sysfs_listing,
    plugged_source,
)

DUMPSYS = """
Current Battery Service state:
  AC powered: false
  USB powered: true
  Wireless powered: false
  Max charging current: 0
  Max charging voltage: 0
  Charge counter: 3279000
  status: 3
  health: 2
  present: true
  level: 73
  scale: 100
  voltage: 3910
  temperature: 312
  technology: Li-ion
"""


def test_parse_dumpsys_battery():
    fields = parse_dumpsys_battery(DUMPSYS)
    assert fields["level"] == 73
    assert fields["usb_powered"] is True
    assert fields["technology"] == "Li-ion"
    assert battery_status(fields["status"]) == "Discharging"
    assert battery_health(fields["health"]) == "Good"
    assert plugged_source(fields) == "USB"
    assert normalize_voltage_v(fields["voltage"]) == pytest.approx(3.91)
    assert normalize_temperature_c(fields["temperature"]) == pytest.approx(31.2)


def test_status_and_health_codes_map_to_names():
    assert battery_status(2) == "Charging"
    assert battery_status(5) == "Full"
    assert battery_status(99) == "99"  # unknown codes pass through as text
    assert battery_status(None) is None
    assert battery_health(7) == "Cold"
    assert battery_health("Good") == "Good"


def test_plugged_source_defaults_to_none():
    assert plugged_source({"ac_powered": False, "usb_powered": False}) == "none"
    assert plugged_source({"ac_powered": True, "usb_powered": True}) == "AC"


def test_parse_adb_devices_with_transport_id():
    devices = parse_adb_devices(
        """
List of devices attached
R5CT123ABC device product:a17 model:SM_A176B device:a17 transport_id:4
192.168.1.50:5555 offline transport_id:6
"""
    )
    assert len(devices) == 2
    assert devices[0].serial == "R5CT123ABC"
    assert devices[0].transport_id == "4"
    assert devices[1].state == "offline"


def test_parse_sysfs_listing():
    listing = parse_sysfs_listing("capacity\ncurrent_now\nstatus\ntemp\nvoltage_now\n")
    assert {"capacity", "current_now", "status", "temp", "voltage_now"} == listing
    assert parse_sysfs_listing("") == set()


def test_sysfs_unit_normalization():
    assert normalize_voltage_v("3910000") == pytest.approx(3.91)  # microvolt (sysfs)
    assert normalize_voltage_v("3910") == pytest.approx(3.91)  # millivolt (dumpsys)
    assert normalize_current_a("-420000") == pytest.approx(-0.42)  # microamp
    assert normalize_current_a("420") == pytest.approx(0.42)  # vendor milliamp
    assert normalize_temperature_c("312") == pytest.approx(31.2)  # tenths of degC
    assert normalize_temperature_c("31") == pytest.approx(31.0)
    assert normalize_capacity_pct("73") == pytest.approx(73.0)
    assert normalize_voltage_v("not-a-number") is None
    assert normalize_current_a(None) is None


def test_capacity_is_clamped_to_percent_range():
    assert normalize_capacity_pct("-5") == 0.0
    assert normalize_capacity_pct("105") == 100.0


def test_dumpsys_soc_pct_uses_scale():
    assert dumpsys_soc_pct({"level": 73, "scale": 100}) == pytest.approx(73.0)
    assert dumpsys_soc_pct({"level": 40, "scale": 80}) == pytest.approx(50.0)
    assert dumpsys_soc_pct({"scale": 100}) is None
