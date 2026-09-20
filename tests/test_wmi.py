"""WmiBatterySource tests, run on any OS against a fake ``wmi`` module.

No test here touches Windows, COM, or the real ``wmi`` package (which is not
even installed on CI): a fake module is injected into ``sys.modules`` before
the source imports it, so the class enumeration, the per-class failures that
real firmware produces, and the ``root\\cimv2`` fallback are all reproducible
on Linux.
"""

from __future__ import annotations

import sys
import threading
from typing import Any

import pytest

from battfeed.sources import wmi_battery
from battfeed.sources.wmi_battery import UNKNOWN_SENTINEL, WmiBatterySource

# --- fake wmi module ------------------------------------------------------


class FakeWmiError(Exception):
    """Stands in for ``wmi.x_wmi`` -- COM failures are not a known type."""


class FakeRow:
    """A WMI instance: attributes, plus optionally an exploding property."""

    def __init__(self, exploding: tuple[str, ...] = (), **fields: Any) -> None:
        self._fields = fields
        self._exploding = exploding

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._exploding:
            raise FakeWmiError(f"Generic failure reading {name}")
        try:
            return self._fields[name]
        except KeyError:
            raise AttributeError(name) from None


class FakeConnection:
    """One namespace. Classes map to rows, or to an exception to raise."""

    def __init__(self, classes: dict[str, Any]) -> None:
        self._classes = classes

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._classes:
            # python-wmi raises when the namespace has no such class at all.
            raise FakeWmiError(f"Invalid class: {name}")
        value = self._classes[name]
        if isinstance(value, Exception):

            def _raise() -> list[Any]:
                raise value

            return _raise
        return lambda: list(value)


class FakeWmiModule:
    """The ``wmi`` package's whole surface as far as this source is concerned."""

    def __init__(self, namespaces: dict[str, dict[str, Any]]) -> None:
        self._namespaces = namespaces
        self.connected: list[str] = []

    def WMI(self, namespace: str | None = None, **_: Any) -> FakeConnection:
        key = namespace or "root\\cimv2"
        self.connected.append(key)
        if key not in self._namespaces:
            raise FakeWmiError(f"namespace {key} not available")
        return FakeConnection(self._namespaces[key])


def _status(**overrides: Any) -> FakeRow:
    """One BatteryStatus row shaped like this laptop's (charging, 84%)."""
    fields: dict[str, Any] = {
        "InstanceName": "ACPI\\PNP0C0A\\1_0",
        "Tag": 21,
        "Voltage": 16844,
        "ChargeRate": 29292,
        "DischargeRate": 0,
        "RemainingCapacity": 52913,
    }
    fields.update(overrides)
    return FakeRow(**fields)


def _install(monkeypatch, root_wmi: dict[str, Any], cimv2: dict[str, Any] | None = None):
    """Inject a fake ``wmi`` module and return it."""
    namespaces = {"root\\wmi": root_wmi}
    if cimv2 is not None:
        namespaces["root\\cimv2"] = cimv2
    module = FakeWmiModule(namespaces)
    monkeypatch.setitem(sys.modules, "wmi", module)
    return module


def _healthy(monkeypatch, **extra: Any):
    """The common case: status + full-charge capacity + cycle count present."""
    classes: dict[str, Any] = {
        "BatteryStatus": [_status()],
        "BatteryFullChargedCapacity": [
            FakeRow(InstanceName="ACPI\\PNP0C0A\\1_0", Tag=21, FullChargedCapacity=62903)
        ],
        "BatteryCycleCount": [FakeRow(InstanceName="ACPI\\PNP0C0A\\1_0", Tag=21, CycleCount=352)],
    }
    classes.update(extra)
    return _install(monkeypatch, classes)


# --- dependency and availability guards -----------------------------------


def test_missing_wmi_dependency_raises_helpful_importerror(monkeypatch):
    monkeypatch.setitem(sys.modules, "wmi", None)  # force `import wmi` to fail
    with pytest.raises(ImportError, match=r"battfeed\[wmi\]"):
        WmiBatterySource()


def test_availability_reports_non_windows_platforms(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert WmiBatterySource.availability() == "requires Windows"


# --- existing behaviour must survive the enrichment -----------------------


def test_poll_keeps_voltage_power_and_derived_current(monkeypatch):
    _healthy(monkeypatch)
    sample = WmiBatterySource().poll()[0]
    assert sample["voltage_volt"] == pytest.approx(16.844)
    assert sample["power_watt"] == pytest.approx(29.292)  # charging -> positive
    assert sample["current_ampere"] == pytest.approx(29.292 / 16.844)
    assert sample["current_ampere"] > 0


def test_poll_signs_discharge_negative(monkeypatch):
    _healthy(monkeypatch, BatteryStatus=[_status(ChargeRate=0, DischargeRate=18000)])
    sample = WmiBatterySource().poll()[0]
    assert sample["power_watt"] == pytest.approx(-18.0)
    assert sample["current_ampere"] < 0


def test_poll_survives_zero_voltage_without_dividing_by_zero(monkeypatch):
    _healthy(monkeypatch, BatteryStatus=[_status(Voltage=0)])
    sample = WmiBatterySource().poll()[0]
    assert sample["voltage_volt"] == 0.0
    assert sample["current_ampere"] == 0.0


def test_poll_returns_one_sample_per_pack(monkeypatch):
    _install(
        monkeypatch,
        {
            "BatteryStatus": [
                _status(InstanceName="PACK0", Tag=1),
                _status(InstanceName="PACK1", Tag=2, Voltage=11000),
            ],
            "BatteryFullChargedCapacity": [
                FakeRow(InstanceName="PACK0", Tag=1, FullChargedCapacity=62903),
                FakeRow(InstanceName="PACK1", Tag=2, FullChargedCapacity=60000),
            ],
        },
    )
    samples = WmiBatterySource().poll()
    assert [s["voltage_volt"] for s in samples] == [pytest.approx(16.844), pytest.approx(11.0)]
    assert samples[0]["state_of_charge_percent"] == pytest.approx(100.0 * 52913 / 62903)
    assert samples[1]["state_of_charge_percent"] == pytest.approx(100.0 * 52913 / 60000)


# --- state of charge ------------------------------------------------------


def test_state_of_charge_from_remaining_over_full_charged(monkeypatch):
    _healthy(monkeypatch)
    sample = WmiBatterySource().poll()[0]
    assert sample["state_of_charge_percent"] == pytest.approx(100.0 * 52913 / 62903)


def test_state_of_charge_matches_instances_by_name_not_position(monkeypatch):
    """The two classes need not enumerate their packs in the same order."""
    _install(
        monkeypatch,
        {
            "BatteryStatus": [
                _status(InstanceName="PACK0", Tag=1, RemainingCapacity=1000),
                _status(InstanceName="PACK1", Tag=2, RemainingCapacity=1000),
            ],
            "BatteryFullChargedCapacity": [
                FakeRow(InstanceName="PACK1", Tag=2, FullChargedCapacity=4000),
                FakeRow(InstanceName="PACK0", Tag=1, FullChargedCapacity=2000),
            ],
        },
    )
    samples = WmiBatterySource().poll()
    assert samples[0]["state_of_charge_percent"] == pytest.approx(50.0)
    assert samples[1]["state_of_charge_percent"] == pytest.approx(25.0)


def test_state_of_charge_omitted_when_capacity_class_fails(monkeypatch):
    _healthy(monkeypatch, BatteryFullChargedCapacity=FakeWmiError("Generic failure"))
    sample = WmiBatterySource().poll()[0]
    assert "state_of_charge_percent" not in sample
    assert sample["voltage_volt"] == pytest.approx(16.844)  # the rest still works


def test_state_of_charge_omitted_when_full_charge_capacity_is_zero(monkeypatch):
    _healthy(
        monkeypatch,
        BatteryFullChargedCapacity=[
            FakeRow(InstanceName="ACPI\\PNP0C0A\\1_0", Tag=21, FullChargedCapacity=0)
        ],
    )
    assert "state_of_charge_percent" not in WmiBatterySource().poll()[0]


def test_state_of_charge_omitted_when_instance_counts_mismatch(monkeypatch):
    """Two packs, one keyless capacity row: guessing would mislabel a pack."""
    _install(
        monkeypatch,
        {
            "BatteryStatus": [
                _status(InstanceName=None, Tag=None),
                _status(InstanceName=None, Tag=None),
            ],
            "BatteryFullChargedCapacity": [FakeRow(FullChargedCapacity=62903)],
        },
    )
    assert all("state_of_charge_percent" not in s for s in WmiBatterySource().poll())


def test_state_of_charge_clamped_to_100(monkeypatch):
    _healthy(monkeypatch, BatteryStatus=[_status(RemainingCapacity=70000)])
    assert WmiBatterySource().poll()[0]["state_of_charge_percent"] == 100.0


# --- the 0x80000000 "unknown" sentinel ------------------------------------


@pytest.mark.parametrize("sentinel", [UNKNOWN_SENTINEL, -UNKNOWN_SENTINEL])
def test_unknown_rate_sentinel_is_absent_not_a_number(monkeypatch, sentinel):
    _healthy(monkeypatch, BatteryStatus=[_status(ChargeRate=sentinel, DischargeRate=0)])
    sample = WmiBatterySource().poll()[0]
    assert sample["power_watt"] == 0.0  # not 2 147 483 W
    assert sample["current_ampere"] == 0.0


def test_unknown_remaining_capacity_suppresses_state_of_charge(monkeypatch):
    _healthy(monkeypatch, BatteryStatus=[_status(RemainingCapacity=UNKNOWN_SENTINEL)])
    assert "state_of_charge_percent" not in WmiBatterySource().poll()[0]


def test_unknown_full_charged_capacity_suppresses_state_of_charge(monkeypatch):
    _healthy(
        monkeypatch,
        BatteryFullChargedCapacity=[
            FakeRow(InstanceName="ACPI\\PNP0C0A\\1_0", Tag=21, FullChargedCapacity=UNKNOWN_SENTINEL)
        ],
    )
    assert "state_of_charge_percent" not in WmiBatterySource().poll()[0]


def test_unknown_voltage_sentinel_does_not_become_a_voltage(monkeypatch):
    _healthy(monkeypatch, BatteryStatus=[_status(Voltage=UNKNOWN_SENTINEL)])
    sample = WmiBatterySource().poll()[0]
    assert sample["voltage_volt"] == 0.0
    assert sample["current_ampere"] == 0.0


# --- cycle count ----------------------------------------------------------


def test_cycle_count_emitted_when_the_class_exists(monkeypatch):
    _healthy(monkeypatch)
    sample = WmiBatterySource().poll()[0]
    assert sample["cycle_count"] == 352
    assert isinstance(sample["cycle_count"], int)


def test_cycle_count_omitted_when_the_class_is_absent(monkeypatch):
    _install(
        monkeypatch,
        {
            "BatteryStatus": [_status()],
            "BatteryFullChargedCapacity": [FakeRow(InstanceName="ACPI\\PNP0C0A\\1_0", Tag=21)],
        },
    )
    assert "cycle_count" not in WmiBatterySource().poll()[0]


def test_cycle_count_class_is_probed_only_once(monkeypatch):
    """Firmware without the class never grows it; asking every poll is waste."""
    calls: list[str] = []

    class CountingConnection(FakeConnection):
        def __getattr__(self, name: str) -> Any:
            if not name.startswith("_"):
                calls.append(name)
            return super().__getattr__(name)

    class CountingModule(FakeWmiModule):
        def WMI(self, namespace: str | None = None, **kwargs: Any) -> FakeConnection:
            connection = super().WMI(namespace=namespace, **kwargs)
            return CountingConnection(connection._classes)

    module = CountingModule(
        {
            "root\\wmi": {
                "BatteryStatus": [_status()],
                "BatteryFullChargedCapacity": [
                    FakeRow(InstanceName="ACPI\\PNP0C0A\\1_0", Tag=21, FullChargedCapacity=62903)
                ],
                "BatteryCycleCount": FakeWmiError("Invalid class"),
            }
        }
    )
    monkeypatch.setitem(sys.modules, "wmi", module)
    source = WmiBatterySource()
    source.poll()
    source.poll()
    assert calls.count("BatteryCycleCount") == 1


def test_unknown_cycle_count_sentinel_is_omitted(monkeypatch):
    _healthy(
        monkeypatch,
        BatteryCycleCount=[
            FakeRow(InstanceName="ACPI\\PNP0C0A\\1_0", Tag=21, CycleCount=UNKNOWN_SENTINEL)
        ],
    )
    assert "cycle_count" not in WmiBatterySource().poll()[0]


# --- metadata -------------------------------------------------------------

STATIC_DATA = FakeRow(
    InstanceName="ACPI\\PNP0C0A\\1_0",
    Tag=21,
    DesignedCapacity=70000,
    DesignedVoltage=16851,
    Chemistry=(76, 73, 79, 78),  # "LION", as the uint8[4] the MOF declares
    DeviceName="Primary",
    SerialNumber="01384",
    ManufactureName="Hewlett-Packard",
)


def test_metadata_keeps_its_static_description(monkeypatch):
    _healthy(monkeypatch)
    info = WmiBatterySource().metadata()
    assert info["source"] == "wmi"
    assert info["kind"] == "wmi-battery"
    assert info["namespace"] == "root\\wmi"
    assert info["os_host"]  # platform.node(), always a string on a real host


def test_metadata_exposes_static_data_fields(monkeypatch):
    _healthy(monkeypatch, BatteryStaticData=[STATIC_DATA])
    info = WmiBatterySource().metadata()
    assert info["design_capacity_mwh"] == 70000
    assert info["design_voltage_mv"] == 16851
    assert info["full_charged_capacity_mwh"] == 62903
    assert info["chemistry"] == "LION"
    assert info["device_name"] == "Primary"
    assert info["serial"] == "01384"
    assert info["manufacturer"] == "Hewlett-Packard"
    assert info["cycle_count"] == 352
    assert info["battery_count"] == 1
    assert info["packs"][0]["static_data_available"] is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("LiP", "LiP", id="already-decoded string"),
        pytest.param((76, 73, 111, 110), "LIon", id="uint8[4] tuple"),
        pytest.param(b"LIon", "LIon", id="bytes"),
        # What python-wmi actually returns on the HP laptop: the uint8[4]
        # packed into one integer, b"LIon" read little-endian.
        pytest.param(1852787020, "LIon", id="packed integer"),
        pytest.param(6, None, id="not a chemistry code at all"),
    ],
)
def test_metadata_decodes_every_chemistry_shape(monkeypatch, raw, expected):
    _healthy(
        monkeypatch,
        BatteryStaticData=[FakeRow(InstanceName="ACPI\\PNP0C0A\\1_0", Chemistry=raw)],
    )
    assert WmiBatterySource().metadata().get("chemistry") == expected


def test_metadata_flags_relative_capacity_packs(monkeypatch):
    """A pack reporting unitless capacity must not pass as milliwatt-hour."""
    _healthy(
        monkeypatch,
        BatteryStaticData=[FakeRow(InstanceName="ACPI\\PNP0C0A\\1_0", Capabilities=0x40000000)],
    )
    assert WmiBatterySource().metadata()["capacity_relative"] is True


def test_metadata_omits_the_relative_flag_for_ordinary_packs(monkeypatch):
    """0x80000000 is BATTERY_SYSTEM_BATTERY here, not the relative bit."""
    _healthy(
        monkeypatch,
        BatteryStaticData=[FakeRow(InstanceName="ACPI\\PNP0C0A\\1_0", Capabilities=-2147483648)],
    )
    assert "capacity_relative" not in WmiBatterySource().metadata()


def test_metadata_falls_back_to_win32_battery_when_static_data_fails(monkeypatch):
    """Verified on Simon's machine: BatteryStaticData -> 'Generic failure'."""
    _install(
        monkeypatch,
        {
            "BatteryStatus": [_status()],
            "BatteryFullChargedCapacity": [
                FakeRow(InstanceName="ACPI\\PNP0C0A\\1_0", Tag=21, FullChargedCapacity=62903)
            ],
            "BatteryCycleCount": [
                FakeRow(InstanceName="ACPI\\PNP0C0A\\1_0", Tag=21, CycleCount=352)
            ],
            "BatteryStaticData": FakeWmiError("Generic failure"),
        },
        cimv2={
            "Win32_Battery": [
                FakeRow(Name="Primary", DesignVoltage=16851, Chemistry=6, DesignCapacity=None)
            ]
        },
    )
    info = WmiBatterySource().metadata()
    assert info["design_voltage_mv"] == 16851
    assert info["device_name"] == "Primary"
    assert info["chemistry"] == "lithium-ion"
    assert info["full_charged_capacity_mwh"] == 62903  # still from root\wmi
    assert info["cycle_count"] == 352
    assert "design_capacity_mwh" not in info  # Win32_Battery has no value for it
    assert "serial" not in info  # nor a serial or manufacturer
    assert "manufacturer" not in info
    assert info["packs"][0]["static_data_available"] is False


def test_metadata_survives_a_single_exploding_static_property(monkeypatch):
    """A readable row can still have one property raise; only that one is lost."""
    _healthy(
        monkeypatch,
        BatteryStaticData=[
            FakeRow(
                exploding=("SerialNumber",),
                InstanceName="ACPI\\PNP0C0A\\1_0",
                DesignedCapacity=70000,
                DeviceName="Primary",
            )
        ],
    )
    info = WmiBatterySource().metadata()
    assert info["design_capacity_mwh"] == 70000
    assert info["device_name"] == "Primary"
    assert "serial" not in info


def test_metadata_mixes_static_data_with_the_win32_fallback(monkeypatch):
    """The shape this laptop actually reports: DesignedVoltage alone raises."""
    _install(
        monkeypatch,
        {
            "BatteryStatus": [_status()],
            "BatteryStaticData": [
                FakeRow(
                    exploding=("DesignedVoltage",),
                    InstanceName="ACPI\\PNP0C0A\\1_0",
                    DesignedCapacity=94338,
                    Chemistry=1852787020,
                    DeviceName="Primary",
                    SerialNumber="01384 2021/06/09",
                    ManufactureName="Hewlett-Packard",
                )
            ],
        },
        cimv2={"Win32_Battery": [FakeRow(Name="Primary", DesignVoltage=16851, Chemistry=2)]},
    )
    info = WmiBatterySource().metadata()
    assert info["design_capacity_mwh"] == 94338
    assert info["chemistry"] == "LIon"  # the static-data code wins over Win32's "Unknown"
    assert info["design_voltage_mv"] == 16851  # only the missing field is filled in
    assert info["manufacturer"] == "Hewlett-Packard"


def test_metadata_omits_unreadable_chemistry_codes(monkeypatch):
    """Win32_Battery code 2 is 'Unknown'; reporting that would be noise."""
    _install(
        monkeypatch,
        {"BatteryStatus": [_status()], "BatteryStaticData": FakeWmiError("Generic failure")},
        cimv2={"Win32_Battery": [FakeRow(Name="Primary", Chemistry=2)]},
    )
    info = WmiBatterySource().metadata()
    assert "chemistry" not in info
    assert info["device_name"] == "Primary"


def test_metadata_tolerates_a_machine_without_a_battery(monkeypatch):
    _install(monkeypatch, {"BatteryStatus": []})
    info = WmiBatterySource().metadata()
    assert info["battery_count"] == 0
    assert info["packs"] == []


def test_metadata_tolerates_a_missing_cimv2_namespace(monkeypatch):
    _install(monkeypatch, {"BatteryStatus": [_status()]})  # no root\cimv2 registered
    info = WmiBatterySource().metadata()
    assert info["battery_count"] == 1
    assert "chemistry" not in info


def test_metadata_lists_every_pack_and_promotes_the_first(monkeypatch):
    _install(
        monkeypatch,
        {
            "BatteryStatus": [_status(InstanceName="PACK0"), _status(InstanceName="PACK1")],
            "BatteryStaticData": [
                FakeRow(InstanceName="PACK0", DeviceName="Main"),
                FakeRow(InstanceName="PACK1", DeviceName="Slice"),
            ],
        },
    )
    info = WmiBatterySource().metadata()
    assert info["battery_count"] == 2
    assert info["device_name"] == "Main"
    assert [pack["device_name"] for pack in info["packs"]] == ["Main", "Slice"]


# --- discovery and instance selection -------------------------------------


def test_discover_lists_installed_packs(monkeypatch):
    _healthy(monkeypatch, BatteryStaticData=[STATIC_DATA])
    candidates = WmiBatterySource.discover()
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["option"] == "instance"
    assert candidate["value"] == 0
    assert candidate["device_name"] == "Primary"
    assert candidate["manufacturer"] == "Hewlett-Packard"
    assert candidate["full_charged_capacity_mwh"] == 62903
    assert candidate["cycle_count"] == 352


def test_discover_indexes_multiple_packs(monkeypatch):
    _install(
        monkeypatch,
        {"BatteryStatus": [_status(InstanceName="PACK0"), _status(InstanceName="PACK1")]},
    )
    candidates = WmiBatterySource.discover()
    assert [c["value"] for c in candidates] == [0, 1]
    assert [c["instance_name"] for c in candidates] == ["PACK0", "PACK1"]


def test_discover_on_a_machine_without_a_battery(monkeypatch):
    _install(monkeypatch, {"BatteryStatus": []})
    assert WmiBatterySource.discover() == []


def test_discover_still_lists_packs_when_static_data_fails(monkeypatch):
    _install(
        monkeypatch,
        {"BatteryStatus": [_status()], "BatteryStaticData": FakeWmiError("Generic failure")},
    )
    candidate = WmiBatterySource.discover()[0]
    assert candidate["value"] == 0
    assert candidate["instance_name"] == "ACPI\\PNP0C0A\\1_0"
    assert candidate["device_name"] is None


def test_instance_index_pins_a_single_pack(monkeypatch):
    _install(
        monkeypatch,
        {
            "BatteryStatus": [
                _status(InstanceName="PACK0", Voltage=16000),
                _status(InstanceName="PACK1", Voltage=11000),
            ]
        },
    )
    samples = WmiBatterySource(instance=1).poll()
    assert len(samples) == 1
    assert samples[0]["voltage_volt"] == pytest.approx(11.0)


def test_instance_name_pins_a_single_pack(monkeypatch):
    _install(
        monkeypatch,
        {
            "BatteryStatus": [
                _status(InstanceName="PACK0", Voltage=16000),
                _status(InstanceName="PACK1", Voltage=11000),
            ]
        },
    )
    samples = WmiBatterySource(instance="pack1").poll()  # matching is case-insensitive
    assert [s["voltage_volt"] for s in samples] == [pytest.approx(11.0)]


def test_unknown_instance_fails_at_construction(monkeypatch):
    _healthy(monkeypatch)
    with pytest.raises(ValueError, match="matches no installed battery pack"):
        WmiBatterySource(instance=3)


def test_metadata_promotes_the_pinned_pack(monkeypatch):
    _install(
        monkeypatch,
        {
            "BatteryStatus": [_status(InstanceName="PACK0"), _status(InstanceName="PACK1")],
            "BatteryStaticData": [
                FakeRow(InstanceName="PACK0", DeviceName="Main"),
                FakeRow(InstanceName="PACK1", DeviceName="Slice"),
            ],
        },
    )
    assert WmiBatterySource(instance=1).metadata()["device_name"] == "Slice"


# --- protocol conformance -------------------------------------------------


def test_source_satisfies_the_datasource_protocol(monkeypatch):
    from battfeed.protocols import DataSource

    _healthy(monkeypatch)
    assert isinstance(WmiBatterySource(), DataSource)


def test_samples_carry_only_canonical_bdf_keys(monkeypatch):
    _healthy(monkeypatch, BatteryStaticData=[STATIC_DATA])
    sample = WmiBatterySource().poll()[0]
    assert set(sample) == {
        "voltage_volt",
        "current_ampere",
        "power_watt",
        "state_of_charge_percent",
        "cycle_count",
    }
    assert all(isinstance(value, (int, float)) for value in sample.values())


def test_sentinel_constant_is_the_documented_value():
    assert wmi_battery.UNKNOWN_SENTINEL == 0x80000000 == 2147483648


# --- threading ------------------------------------------------------------


def test_poll_from_another_thread_makes_its_own_connection(monkeypatch):
    """A harvester loop runs on its own thread; COM handles cannot cross one.

    Reusing the constructor's connection raises "CoInitialize has not been
    called" on real Windows, which left the desktop app collecting nothing.
    """
    module = _healthy(monkeypatch)
    source = WmiBatterySource()
    assert module.connected == ["root\\wmi"]

    results: list[Any] = []

    def worker() -> None:
        try:
            results.append(source.poll())
        except Exception as exc:  # noqa: BLE001 -- reported through the assert below
            results.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert len(results) == 1 and not isinstance(results[0], Exception), results
    assert results[0][0]["voltage_volt"] == pytest.approx(16.844)
    # A second connection, made by and for the worker thread.
    assert module.connected == ["root\\wmi", "root\\wmi"]


def test_each_thread_keeps_its_own_connection_across_polls(monkeypatch):
    """Thread-local caching, not a fresh connection on every single poll."""
    module = _healthy(monkeypatch)
    source = WmiBatterySource()

    def worker() -> None:
        source.poll()
        source.poll()
        source.poll()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=10)

    assert module.connected == ["root\\wmi", "root\\wmi"]
