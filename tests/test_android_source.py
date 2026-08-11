"""AndroidBatterySource tests against an in-memory fake ADB backend.

No test here ever launches an ``adb`` subprocess.
"""

from __future__ import annotations

import shutil
from typing import Sequence

import pytest

from battfeed.protocols import DataSource
from battfeed.sources.android import AndroidBatterySource
from battfeed.sources.android.adb import ADBCommandError, AdbDevice, ADBDeviceDisconnected, ADBError
from battfeed.sources.android.source import _signed_current

DUMPSYS_DISCHARGING = """
Current Battery Service state:
  AC powered: false
  USB powered: false
  Wireless powered: false
  status: 3
  health: 2
  present: true
  level: 73
  scale: 100
  voltage: 3910
  temperature: 312
  technology: Li-ion
"""

DUMPSYS_CHARGING = DUMPSYS_DISCHARGING.replace("status: 3", "status: 2").replace(
    "USB powered: false", "USB powered: true"
)


class FakeAdbBackend:
    """In-memory AdbBackend: canned dumpsys/getprop/sysfs outputs per device."""

    def __init__(self, devices: list[dict]) -> None:
        self.devices: dict[str, dict] = {}
        self.list_devices_calls = 0
        self.shell_calls: list[tuple[str, ...]] = []
        for spec in devices:
            serial = spec["serial"]
            self.devices[serial] = {
                "device": AdbDevice(
                    serial=serial,
                    state=spec.get("state", "device"),
                    transport_id=spec.get("transport_id"),
                ),
                "props": spec.get("props", {}),
                "dumpsys": spec.get("dumpsys", ""),
                "sysfs": spec.get("sysfs", {}),
                "sysfs_blocked": spec.get("sysfs_blocked", False),
                "disconnected": spec.get("disconnected", False),
            }

    def list_devices(self) -> list[AdbDevice]:
        self.list_devices_calls += 1
        return [state["device"] for state in self.devices.values()]

    def shell(self, device: AdbDevice, args: Sequence[str], timeout: float | None = None) -> str:
        del timeout
        self.shell_calls.append(tuple(args))
        state = self.devices.get(device.serial)
        if state is None or state.get("disconnected"):
            raise ADBDeviceDisconnected(f"{device.serial}: device not found")
        if list(args[:2]) == ["dumpsys", "battery"]:
            return str(state["dumpsys"])
        if list(args[:1]) == ["getprop"] and len(args) >= 2:
            return str(state["props"].get(args[1], "")) + "\n"
        if list(args[:1]) == ["ls"]:
            if state["sysfs_blocked"]:
                raise ADBCommandError(f"{device.serial}: ls: permission denied")
            return "\n".join(sorted(state["sysfs"].keys())) + "\n"
        if list(args[:1]) == ["cat"] and len(args) >= 2:
            field = str(args[1]).rstrip("/").split("/")[-1]
            if field not in state["sysfs"]:
                raise ADBCommandError(f"{device.serial}: no such sysfs field {field}")
            return str(state["sysfs"][field]) + "\n"
        raise ADBCommandError(f"unsupported fake adb shell command: {' '.join(args)}")


def _device_spec(**overrides) -> dict:
    spec = {
        "serial": "A17ADB",
        "transport_id": "1",
        "props": {
            "ro.product.manufacturer": "samsung",
            "ro.product.model": "Galaxy A17",
            "ro.build.version.release": "15",
            "ro.serialno": "A17SERIAL",
        },
        "dumpsys": DUMPSYS_DISCHARGING,
        "sysfs": {
            "voltage_now": "3910000",
            "current_now": "420000",
            "temp": "312",
            "capacity": "73",
            "status": "Discharging",
            "health": "Good",
        },
    }
    spec.update(overrides)
    return spec


def test_poll_returns_one_sample_with_canonical_columns():
    source = AndroidBatterySource(backend=FakeAdbBackend([_device_spec()]))

    assert source.name == "android"
    assert isinstance(source, DataSource)  # structural battfeed contract

    samples = source.poll()
    assert len(samples) == 1
    sample = samples[0]
    assert set(sample) == {
        "voltage_volt",
        "current_ampere",
        "surface_temperature_celsius",
        "charge_status",
    }
    assert sample["voltage_volt"] == pytest.approx(3.91)  # 3910000 uV -> V
    assert sample["surface_temperature_celsius"] == pytest.approx(31.2)  # 312 tenths
    assert sample["charge_status"] == "Discharging"
    # 420000 uA raw is positive, but the device is discharging: BDF sign wins.
    assert sample["current_ampere"] == pytest.approx(-0.42)


def test_charging_status_forces_positive_current():
    # Vendor reports charging current as negative; status says Charging.
    spec = _device_spec(dumpsys=DUMPSYS_CHARGING)
    spec["sysfs"] = dict(spec["sysfs"], current_now="-250000", status="Charging")
    source = AndroidBatterySource(backend=FakeAdbBackend([spec]))

    (sample,) = source.poll()
    assert sample["charge_status"] == "Charging"
    assert sample["current_ampere"] == pytest.approx(0.25)


def test_full_and_not_charging_keep_reported_sign():
    # "Full" keeps the trickle value as reported.
    assert _signed_current(0.012, "Full") == pytest.approx(0.012)
    assert _signed_current(-0.012, "Full") == pytest.approx(-0.012)
    # "Not charging" must not trip a substring match on "charging".
    assert _signed_current(-0.015, "Not charging") == pytest.approx(-0.015)
    assert _signed_current(0.1, None) == pytest.approx(0.1)
    assert _signed_current(None, "Charging") is None


def test_dumpsys_fallback_when_sysfs_is_blocked():
    # Samsung tablets SELinux-block sysfs for the shell user; dumpsys still
    # exposes "current now" (positive raw here, forced negative by status).
    dumpsys = DUMPSYS_DISCHARGING + "  current now: 389000\n"
    spec = _device_spec(dumpsys=dumpsys, sysfs_blocked=True)
    source = AndroidBatterySource(backend=FakeAdbBackend([spec]))

    (sample,) = source.poll()
    assert sample["voltage_volt"] == pytest.approx(3.91)  # dumpsys mV -> V
    assert sample["surface_temperature_celsius"] == pytest.approx(31.2)
    assert sample["current_ampere"] == pytest.approx(-0.389)
    assert sample["charge_status"] == "Discharging"


def test_use_sysfs_false_never_touches_sysfs():
    backend = FakeAdbBackend([_device_spec()])
    source = AndroidBatterySource(use_sysfs=False, backend=backend)

    (sample,) = source.poll()
    assert sample["voltage_volt"] == pytest.approx(3.91)  # from dumpsys, not sysfs
    assert not any(call[0] in ("ls", "cat") for call in backend.shell_calls)


def test_missing_current_is_omitted_not_faked():
    spec = _device_spec()
    spec["sysfs"] = {"voltage_now": "3910000", "temp": "312", "status": "Discharging"}
    source = AndroidBatterySource(backend=FakeAdbBackend([spec]))

    (sample,) = source.poll()
    assert "current_ampere" not in sample
    assert sample["voltage_volt"] == pytest.approx(3.91)


def test_poll_raises_when_device_disconnects():
    backend = FakeAdbBackend([_device_spec()])
    source = AndroidBatterySource(backend=backend)
    source.poll()  # healthy first poll

    backend.devices["A17ADB"]["disconnected"] = True
    with pytest.raises(ADBDeviceDisconnected):
        source.poll()


def test_poll_raises_when_no_ready_device():
    source = AndroidBatterySource(backend=FakeAdbBackend([]))
    with pytest.raises(ADBDeviceDisconnected, match="no ready Android device"):
        source.poll()

    offline = AndroidBatterySource(backend=FakeAdbBackend([_device_spec(state="offline")]))
    with pytest.raises(ADBDeviceDisconnected):
        offline.poll()


def test_poll_requires_serial_when_several_devices_are_connected():
    two = FakeAdbBackend([_device_spec(), _device_spec(serial="TABADB", transport_id="2")])
    source = AndroidBatterySource(backend=two)
    with pytest.raises(ADBError, match="A17ADB.*TABADB.*serial"):
        source.poll()


def test_serial_selects_the_requested_device():
    tab = _device_spec(serial="TABADB", transport_id="2")
    tab["sysfs"] = dict(tab["sysfs"], voltage_now="4020000")
    backend = FakeAdbBackend([_device_spec(), tab])

    source = AndroidBatterySource(serial="TABADB", backend=backend)
    (sample,) = source.poll()
    assert sample["voltage_volt"] == pytest.approx(4.02)

    missing = AndroidBatterySource(serial="NOPE", backend=backend)
    with pytest.raises(ADBDeviceDisconnected, match="NOPE"):
        missing.poll()


def test_metadata_shape_and_caching():
    backend = FakeAdbBackend([_device_spec()])
    source = AndroidBatterySource(backend=backend)

    meta = source.metadata()
    assert meta["source"] == "android"
    assert meta["kind"] == "android-adb-battery"
    assert meta["adb_path"] == "adb"
    assert meta["adb_serial"] == "A17ADB"
    assert meta["manufacturer"] == "samsung"
    assert meta["model"] == "Galaxy A17"
    assert meta["android_version"] == "15"
    assert meta["serial"] == "A17SERIAL"
    assert meta["battery_technology"] == "Li-ion"
    assert meta["battery_health"] == "Good"
    assert meta["sysfs_available"] is True

    calls = len(backend.shell_calls)
    assert source.metadata() is meta  # cached: no further adb traffic
    assert len(backend.shell_calls) == calls


def test_metadata_tolerates_probe_failure_and_retries_later():
    backend = FakeAdbBackend([])  # nothing connected yet
    source = AndroidBatterySource(backend=backend)

    partial = source.metadata()
    assert partial["source"] == "android"
    assert partial["manufacturer"] is None
    assert partial["battery_technology"] is None

    # Device shows up later: the failed probe was not cached.
    spec = _device_spec()
    backend.devices["A17ADB"] = FakeAdbBackend([spec]).devices["A17ADB"]
    assert source.metadata()["manufacturer"] == "samsung"


def test_availability_reports_missing_adb(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    assert AndroidBatterySource.availability() == (
        'requires the Android platform-tools "adb" executable on PATH'
    )

    monkeypatch.setattr(shutil, "which", lambda cmd: "C:/platform-tools/adb.exe")
    assert AndroidBatterySource.availability() is None


def test_close_is_idempotent():
    source = AndroidBatterySource(backend=FakeAdbBackend([_device_spec()]))
    source.close()
    source.close()
