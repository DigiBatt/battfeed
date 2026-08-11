"""Device discovery tests: mc3000 BLE scan, android adb enumeration, CLI verb.

No test here ever opens a BLE radio or launches an ``adb`` subprocess: the
BLE scan is faked at the ``_ble_scan`` seam and adb behind an in-memory
backend, mirroring the hardware-free style of the other source tests.
"""

from __future__ import annotations

import json

import pytest

from battfeed import cli
from battfeed.sources.android import AndroidBatterySource
from battfeed.sources.android.adb import AdbDevice, ADBNotFound
from battfeed.sources.android.parser import parse_mdns_services
from battfeed.sources.mc3000 import Mc3000Source
from battfeed.sources.mc3000 import discovery as mc3000_discovery
from battfeed.sources.mc3000 import source as mc3000_source

# --- mc3000 BLE discovery -------------------------------------------------


def test_discover_ble_filters_and_sorts_by_rssi(monkeypatch):
    monkeypatch.setattr(
        mc3000_discovery,
        "_ble_scan",
        lambda timeout_s: [
            ("AA:AA:AA:AA:AA:01", "Charger", -80),
            ("AA:AA:AA:AA:AA:02", "Charger", -55),
            ("AA:AA:AA:AA:AA:03", None, None),
        ],
    )
    candidates = mc3000_discovery.discover_ble()
    assert [c["value"] for c in candidates] == [
        "AA:AA:AA:AA:AA:02",  # strongest signal first
        "AA:AA:AA:AA:AA:01",
        "AA:AA:AA:AA:AA:03",  # unknown RSSI last
    ]
    assert all(c["option"] == "address" for c in candidates)


def test_discover_ble_rejects_bad_timeout():
    with pytest.raises(ValueError, match="timeout_s"):
        mc3000_discovery.discover_ble(timeout_s=0)


def test_mc3000_source_discover_delegates(monkeypatch):
    sentinel = [{"option": "address", "value": "AA:BB", "name": "Charger", "rssi_dbm": -60}]
    monkeypatch.setattr(mc3000_discovery, "discover_ble", lambda timeout_s: sentinel)
    assert Mc3000Source.discover(timeout_s=1.0) == sentinel


def _allow_ble(monkeypatch):
    """Pretend the bleak dependency is installed (no BLE I/O happens here)."""
    monkeypatch.setattr(mc3000_source, "_module_available", lambda name: True)


def test_mc3000_auto_address_resolves_single_candidate(monkeypatch):
    _allow_ble(monkeypatch)
    monkeypatch.setattr(
        mc3000_discovery,
        "discover_ble",
        lambda timeout_s: [{"option": "address", "value": "B0:10:A0:88:79:B7", "rssi_dbm": -60}],
    )
    source = Mc3000Source(transport="ble", address="auto")
    assert source._address == "B0:10:A0:88:79:B7"


def test_mc3000_auto_address_fails_when_none_found(monkeypatch):
    _allow_ble(monkeypatch)
    monkeypatch.setattr(mc3000_discovery, "discover_ble", lambda timeout_s: [])
    with pytest.raises(ValueError, match="no MC3000"):
        Mc3000Source(transport="ble", address="auto")


def test_mc3000_auto_address_fails_when_ambiguous(monkeypatch):
    _allow_ble(monkeypatch)
    monkeypatch.setattr(
        mc3000_discovery,
        "discover_ble",
        lambda timeout_s: [
            {"option": "address", "value": "AA:AA:AA:AA:AA:01", "rssi_dbm": -60},
            {"option": "address", "value": "AA:AA:AA:AA:AA:02", "rssi_dbm": -70},
        ],
    )
    with pytest.raises(ValueError, match="ambiguous"):
        Mc3000Source(transport="ble", address="auto")


def test_mc3000_auto_address_not_scanned_for_mock_transport(monkeypatch):
    def boom(timeout_s):  # pragma: no cover - the assertion is that it never runs
        raise AssertionError("mock transport must not trigger a BLE scan")

    monkeypatch.setattr(mc3000_discovery, "discover_ble", boom)
    source = Mc3000Source(transport="mock", address="auto")  # address is ignored for mock
    assert source.poll()  # the simulated charger works as usual


# --- android adb discovery ------------------------------------------------

MDNS_OUTPUT = """\
List of discovered mdns services
adb-R5GL52NHKJZ-82lRnv\t_adb-tls-connect._tcp\t192.168.1.5:41493
adb-R5GL52NHKJZ-82lRnv\t_adb-tls-pairing._tcp\t192.168.1.5:42123
old-device\t_adb._tcp.\t192.168.1.9:5555
garbage line
short\trow
"""


def test_parse_mdns_services_keeps_connect_rows_only():
    assert parse_mdns_services(MDNS_OUTPUT) == [
        ("adb-R5GL52NHKJZ-82lRnv", "192.168.1.5:41493"),
        ("old-device", "192.168.1.9:5555"),
    ]


def test_parse_mdns_services_empty_input():
    assert parse_mdns_services("") == []
    assert parse_mdns_services("List of discovered mdns services\n") == []


class FakeDiscoveryBackend:
    """list_devices + mdns_services only -- discovery never calls shell()."""

    def __init__(self, devices: list[AdbDevice], mdns_text: str = "") -> None:
        self._devices = devices
        self._mdns_text = mdns_text

    def list_devices(self) -> list[AdbDevice]:
        return self._devices

    def shell(self, device, args, timeout=None):  # pragma: no cover - guard
        raise AssertionError("discovery must not shell into devices")

    def mdns_services(self) -> str:
        return self._mdns_text


class NoMdnsBackend(FakeDiscoveryBackend):
    mdns_services = None  # type: ignore[assignment]  # backend without the hook


def test_android_discover_lists_connected_and_advertised():
    backend = FakeDiscoveryBackend(
        devices=[
            AdbDevice(serial="192.168.1.170:32919", qualifiers={"model": "SM_A175F"}),
            AdbDevice(serial="R5GL52NHKJZ", state="offline"),
        ],
        mdns_text=MDNS_OUTPUT,
    )
    candidates = AndroidBatterySource.discover(backend=backend)
    by_value = {c["value"]: c for c in candidates}

    connected = by_value["192.168.1.170:32919"]
    assert connected["ready"] is True and connected["model"] == "SM_A175F"

    offline = by_value["R5GL52NHKJZ"]
    assert offline["ready"] is False and offline["state"] == "offline"

    advertised = by_value["192.168.1.5:41493"]
    assert advertised["ready"] is False
    assert "adb connect 192.168.1.5:41493" in advertised["state"]

    assert all(c["option"] == "serial" for c in candidates)


def test_android_discover_dedupes_mdns_against_connected():
    backend = FakeDiscoveryBackend(
        devices=[AdbDevice(serial="192.168.1.5:41493")],
        mdns_text="adb-x\t_adb-tls-connect._tcp\t192.168.1.5:41493\n",
    )
    candidates = AndroidBatterySource.discover(backend=backend)
    assert [c["value"] for c in candidates] == ["192.168.1.5:41493"]


def test_android_discover_tolerates_backend_without_mdns():
    backend = NoMdnsBackend(devices=[AdbDevice(serial="X")])
    assert [c["value"] for c in AndroidBatterySource.discover(backend=backend)] == ["X"]


def test_android_auto_serial_resolves_single_ready_device():
    backend = FakeDiscoveryBackend(
        devices=[
            AdbDevice(serial="192.168.1.5:41493"),
            AdbDevice(serial="R5GL52NHKJZ", state="offline"),
        ]
    )
    source = AndroidBatterySource(serial="auto", backend=backend)
    assert source._serial == "192.168.1.5:41493"


def test_android_auto_serial_fails_when_none_ready():
    backend = FakeDiscoveryBackend(devices=[AdbDevice(serial="X", state="unauthorized")])
    with pytest.raises(ValueError, match="no ready Android device"):
        AndroidBatterySource(serial="auto", backend=backend)


def test_android_auto_serial_fails_when_ambiguous():
    backend = FakeDiscoveryBackend(devices=[AdbDevice(serial="A"), AdbDevice(serial="B")])
    with pytest.raises(ValueError, match="ambiguous"):
        AndroidBatterySource(serial="auto", backend=backend)


def test_android_auto_serial_wraps_adb_errors_as_usage_errors():
    class BrokenBackend(FakeDiscoveryBackend):
        def list_devices(self):
            raise ADBNotFound("adb executable not found: adb")

    with pytest.raises(ValueError, match="adb executable not found"):
        AndroidBatterySource(serial="auto", backend=BrokenBackend(devices=[]))


# --- CLI verb -------------------------------------------------------------


class StubDiscoverableSource:
    name = "stub"

    def metadata(self):  # pragma: no cover - protocol completeness only
        return {"source": self.name}

    def poll(self):  # pragma: no cover - protocol completeness only
        return []

    @classmethod
    def discover(cls, timeout_s: float = 6.0):
        return [
            {"option": "address", "value": "AA:BB:CC:DD:EE:FF", "name": "Charger"},
            {"option": "address", "value": "1.2.3.4:5", "ready": False, "state": "advertised"},
        ]


class StubUnavailableSource(StubDiscoverableSource):
    name = "stub-unavailable"

    @classmethod
    def availability(cls):
        return "requires hardware missing from this machine"


class StubPlainSource(StubDiscoverableSource):
    name = "stub-plain"
    discover = None  # type: ignore[assignment]  # a source without the hook


def _stub_registry(monkeypatch):
    monkeypatch.setattr(
        cli,
        "available_sources",
        lambda: {
            "stub": StubDiscoverableSource,
            "stub-unavailable": StubUnavailableSource,
            "stub-plain": StubPlainSource,
        },
    )


def test_cli_discover_sweep_prints_candidates_and_notes(monkeypatch, capsys):
    _stub_registry(monkeypatch)
    assert cli.main(["discover"]) == 0
    out = capsys.readouterr().out
    assert "AA:BB:CC:DD:EE:FF" in out
    assert "--opt address=AA:BB:CC:DD:EE:FF" in out  # ready candidate gets a collect hint
    assert "--opt address=1.2.3.4:5" not in out  # not-ready candidate does not
    assert "skipped: requires hardware" in out
    assert "stub-plain" not in out  # no discover hook -> silently skipped in a sweep


def test_cli_discover_named_source_ignores_availability(monkeypatch, capsys):
    """--opt can supply what availability() found missing, so --source always tries."""
    _stub_registry(monkeypatch)
    assert cli.main(["discover", "--source", "stub-unavailable"]) == 0
    out = capsys.readouterr().out
    assert "AA:BB:CC:DD:EE:FF" in out


def test_cli_discover_json_output(monkeypatch, capsys):
    _stub_registry(monkeypatch)
    assert cli.main(["discover", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [c["value"] for c in payload["candidates"]["stub"]] == [
        "AA:BB:CC:DD:EE:FF",
        "1.2.3.4:5",
    ]
    assert "stub-unavailable" in payload["notes"]


def test_cli_discover_named_source_without_hook_fails(monkeypatch, capsys):
    _stub_registry(monkeypatch)
    assert cli.main(["discover", "--source", "stub-plain"]) == 2
    assert "does not support discovery" in capsys.readouterr().err


def test_cli_discover_unknown_source_fails(capsys):
    assert cli.main(["discover", "--source", "nope"]) == 2
    assert "unknown source" in capsys.readouterr().err


def test_cli_discover_opt_requires_source(capsys):
    assert cli.main(["discover", "--opt", "adb_path=x"]) == 2
    assert "requires --source" in capsys.readouterr().err


def test_cli_discover_rejects_bad_timeout(capsys):
    assert cli.main(["discover", "--timeout", "0"]) == 2
    assert "timeout" in capsys.readouterr().err


def test_cli_discover_bad_opt_fails_cleanly(monkeypatch, capsys):
    _stub_registry(monkeypatch)
    assert cli.main(["discover", "--source", "stub", "--opt", "bogus=1"]) == 2
    assert "bad --opt" in capsys.readouterr().err


def test_cli_discover_named_source_scan_failure(monkeypatch, capsys):
    class ExplodingSource(StubDiscoverableSource):
        @classmethod
        def discover(cls, timeout_s: float = 6.0):
            raise RuntimeError("radio on fire")

    monkeypatch.setattr(cli, "available_sources", lambda: {"stub": ExplodingSource})
    assert cli.main(["discover", "--source", "stub"]) == 2
    assert "radio on fire" in capsys.readouterr().err


def test_cli_discover_sweep_reports_scan_failure_as_note(monkeypatch, capsys):
    class ExplodingSource(StubDiscoverableSource):
        @classmethod
        def discover(cls, timeout_s: float = 6.0):
            raise RuntimeError("radio on fire")

    monkeypatch.setattr(cli, "available_sources", lambda: {"stub": ExplodingSource})
    assert cli.main(["discover"]) == 0
    assert "failed: radio on fire" in capsys.readouterr().out
