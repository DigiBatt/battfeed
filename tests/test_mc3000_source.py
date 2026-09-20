"""Mc3000Source tests -- mock transport only: no hardware, no bleak/pyusb."""

from __future__ import annotations

import json
import sys

import pytest

from battfeed.sources.mc3000 import Mc3000Source
from battfeed.sources.mc3000 import source as source_mod
from battfeed.sources.mc3000.protocol import (
    STATUS_CHARGING,
    STATUS_DISCHARGING,
    SlotReading,
)
from battfeed.sources.mc3000.source import sample_from_reading
from battfeed.sources.mc3000.transports.base import Transport, TransportError

#: Default (time_base="collection"): no test_time_second -- the harvester stamps it.
BDF_KEYS = {
    "voltage_volt",
    "current_ampere",
    "cumulative_capacity_ah",
    "surface_temperature_celsius",
}
#: time_base="device": the device's own program run timer rides along.
BDF_KEYS_DEVICE = BDF_KEYS | {"test_time_second"}


def _reading(**overrides) -> SlotReading:
    base = dict(
        slot=0,
        battery_type_code=0,
        mode_code=3,
        program_count=0,
        status_code=STATUS_DISCHARGING,
        elapsed_s=3600,
        voltage_mv=3700,
        current_ma=1500,
        capacity_mah=1234,
        temperature=27,
        resistance_mohm=40,
        led_bits=0,
    )
    base.update(overrides)
    return SlotReading(**base)


class _DeadTransport(Transport):
    """Opens fine, then every poll fails -- an unreachable device."""

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def poll(self, cmd: int, slot: int) -> bytes:
        raise TransportError("device gone")


class _NoOpenTransport(Transport):
    """A device that cannot even be connected to."""

    def open(self) -> None:
        raise TransportError("no device found")

    def close(self) -> None:
        pass

    def poll(self, cmd: int, slot: int) -> bytes:  # pragma: no cover - never reached
        raise TransportError("not open")


# --- import hygiene and availability ----------------------------------------
def test_import_and_availability_do_not_pull_hardware_libs():
    result = Mc3000Source.availability()
    assert result is None or isinstance(result, str)
    # The package import (at the top of this module) and availability() must
    # both work without bleak/pyusb ever being imported.
    assert "bleak" not in sys.modules
    assert "usb" not in sys.modules


def test_availability_names_the_extras_when_deps_missing(monkeypatch):
    monkeypatch.setattr(source_mod, "_module_available", lambda name: False)
    reason = Mc3000Source.availability()
    assert "battfeed[mc3000-ble]" in reason
    assert "battfeed[mc3000-usb]" in reason
    assert "mock" in reason  # the mock transport works without the extras


def test_availability_is_none_when_deps_present(monkeypatch):
    monkeypatch.setattr(source_mod, "_module_available", lambda name: True)
    assert Mc3000Source.availability() is None


# --- construction ------------------------------------------------------------
def test_constructor_validates_slot_and_transport():
    assert Mc3000Source(transport="mock").name == "mc3000"
    with pytest.raises(ValueError, match="slot"):
        Mc3000Source(slot=4, transport="mock")
    with pytest.raises(ValueError, match="slot"):
        Mc3000Source(slot=-1, transport="mock")
    with pytest.raises(ValueError, match="slot"):
        Mc3000Source(slot="1", transport="mock")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="transport"):
        Mc3000Source(transport="serial")
    with pytest.raises(ValueError, match="address"):
        Mc3000Source(transport="ble")  # BLE needs an address, checked before deps
    with pytest.raises(ValueError, match="time_base"):
        Mc3000Source(transport="mock", time_base="wall-clock")


def test_hardware_transports_require_optional_deps(monkeypatch):
    monkeypatch.setattr(source_mod, "_module_available", lambda name: False)
    with pytest.raises(ImportError, match=r"battfeed\[mc3000-ble\]"):
        Mc3000Source(transport="ble", address="AA:BB:CC:DD:EE:FF")
    with pytest.raises(ImportError, match=r"battfeed\[mc3000-usb\]"):
        Mc3000Source(transport="usb")


# --- unit conversion and sign convention (known readings) --------------------
def test_units_and_discharge_sign_on_known_reading():
    sample = sample_from_reading(_reading())  # discharging: 3700 mV, 1500 mA, 1234 mAh
    assert set(sample) == BDF_KEYS  # no test_time_second: the harvester stamps it
    assert sample["voltage_volt"] == 3.7
    assert sample["current_ampere"] == -1.5  # discharge -> negative (BDF)
    assert sample["cumulative_capacity_ah"] == -1.234  # signed like the current
    assert sample["surface_temperature_celsius"] == 27.0


def test_device_time_base_uses_the_program_run_timer():
    sample = sample_from_reading(_reading(), time_base="device")
    assert set(sample) == BDF_KEYS_DEVICE
    assert sample["test_time_second"] == 3600.0  # the device's own run timer


def test_charge_sign_is_positive():
    sample = sample_from_reading(_reading(status_code=STATUS_CHARGING, mode_code=0))
    assert sample["current_ampere"] == 1.5
    assert sample["cumulative_capacity_ah"] == 1.234


def test_paused_slot_reports_zero_current():
    sample = sample_from_reading(_reading(status_code=3, current_ma=0))  # Pause
    assert sample["current_ampere"] == 0.0


# --- polling through the mock transport --------------------------------------
def test_poll_mock_charging_bay():
    source = Mc3000Source(slot=0, transport="mock")  # mock bay 0 charges a LiIon at 1 A
    try:
        first = source.poll()
        second = source.poll()
    finally:
        source.close()
    assert len(first) == len(second) == 1
    sample = first[0]
    # Default timebase: no test_time_second in the sample; the harvester stamps it.
    assert set(sample) == BDF_KEYS
    assert sample["current_ampere"] == 1.0  # charging -> positive
    assert 3.0 < sample["voltage_volt"] < 4.5


def test_poll_mock_with_device_time_base():
    source = Mc3000Source(slot=0, transport="mock", time_base="device")
    try:
        first = source.poll()
        second = source.poll()
    finally:
        source.close()
    # The mock's run timer advances one second per poll.
    assert first[0]["test_time_second"] == 1.0
    assert second[0]["test_time_second"] == 2.0


def test_poll_mock_discharging_bay_is_negative():
    source = Mc3000Source(slot=1, transport="mock")  # mock bay 1 discharges at 0.5 A
    try:
        samples = [source.poll()[0] for _ in range(10)]
    finally:
        source.close()
    assert all(sample["current_ampere"] == -0.5 for sample in samples)
    assert all(sample["cumulative_capacity_ah"] <= 0 for sample in samples)
    assert samples[-1]["cumulative_capacity_ah"] < 0  # enough mAh accumulated to show


def test_poll_empty_bay_returns_no_samples():
    source = Mc3000Source(slot=3, transport="mock")  # mock bay 3 has no cell
    try:
        assert source.poll() == []
    finally:
        source.close()


def test_unreachable_device_raises_for_harvester_backoff(monkeypatch):
    source = Mc3000Source(transport="mock")
    monkeypatch.setattr(source_mod, "build_transport", lambda kind, address=None: _DeadTransport())
    with pytest.raises(TransportError):
        source.poll()


# --- metadata -----------------------------------------------------------------
def test_metadata_shape_and_machine_info():
    source = Mc3000Source(slot=2, transport="mock")
    try:
        meta = source.metadata()
    finally:
        source.close()
    assert meta["source"] == "mc3000"
    assert meta["instrument_model"] == "SkyRC MC3000"
    assert meta["transport"] == "mock"
    assert meta["address"] is None
    assert meta["slot"] == 2
    assert meta["channel"] == 3  # 1-based label printed on the unit
    assert meta["time_base"] == "collection"
    # The mock charger reports a serial; it round-trips the machine-info frame.
    assert bytes.fromhex(meta["serial"]) == b"MOCKMC3000DEV01"
    assert meta["firmware"] is None
    json.dumps(meta)  # must be JSON-serialisable for the .meta.json sidecar


def test_machine_info_never_polled_when_transport_cannot_answer(monkeypatch):
    """A transport that declares supports_machine_info=False (BLE, in practice)
    must not cost a poll-timeout of dead air: the 0x5a request never goes out."""
    from battfeed.sources.mc3000.protocol import CMD_MACHINE_INFO, encode_progress
    from battfeed.sources.mc3000.reader import Mc3000Reader

    class _BleLikeTransport(Transport):
        frame_kind = "ble"
        supports_machine_info = False
        polled: list[int] = []

        def open(self) -> None:
            pass

        def close(self) -> None:
            pass

        def poll(self, cmd: int, slot: int) -> bytes:
            self.polled.append(cmd)
            return encode_progress(_reading(slot=slot))

    transport = _BleLikeTransport()
    assert Mc3000Reader(transport).read_machine_info() is None
    assert CMD_MACHINE_INFO not in transport.polled

    # And through the source: metadata() degrades to serial=None, measurements
    # still flow.
    source = Mc3000Source(transport="mock")
    monkeypatch.setattr(source_mod, "build_transport", lambda kind, address=None: transport)
    try:
        assert source.metadata()["serial"] is None
        assert source.poll()  # slot readout unaffected
    finally:
        source.close()
    assert CMD_MACHINE_INFO not in transport.polled


def test_metadata_tolerates_unreachable_device(monkeypatch):
    source = Mc3000Source(transport="mock")
    monkeypatch.setattr(
        source_mod, "build_transport", lambda kind, address=None: _NoOpenTransport()
    )
    meta = source.metadata()  # must not raise
    assert meta["serial"] is None
    assert meta["firmware"] is None
    assert meta["transport"] == "mock"
    json.dumps(meta)


# --- lifecycle ----------------------------------------------------------------
def test_close_is_idempotent_and_poll_reconnects():
    source = Mc3000Source(slot=0, transport="mock")
    source.close()  # closing before ever connecting is fine
    assert source.poll()  # lazy connect
    source.close()
    source.close()  # second close is a no-op
    assert source.poll()  # a fresh transport is built after close
    source.close()
