"""Poll the battery of the local Windows machine via WMI."""

from __future__ import annotations

import importlib.util
import logging
import platform
import sys
import threading
from typing import Any, Mapping

__all__ = ["WmiBatterySource"]

logger = logging.getLogger(__name__)

#: Namespace holding the ACPI battery classes (``BatteryStatus`` and friends).
WMI_NAMESPACE = "root\\wmi"

#: Namespace holding ``Win32_Battery``, the partial fallback for static data.
CIMV2_NAMESPACE = "root\\cimv2"

#: ACPI reports "this value is unknown" in a rate or capacity field as
#: 0x80000000 rather than as a null, so the raw number is meaningless and must
#: be treated as absent -- a discharge rate of 2147483648 mW is not a reading.
#: Signed readings of the same bit pattern (-2147483648) are recognised too.
UNKNOWN_SENTINEL = 0x80000000

#: ``BatteryStaticData.Capabilities`` bit (ACPI ``BATTERY_CAPACITY_RELATIVE``)
#: set by packs that report capacity as a unitless relative number instead of
#: in milliwatt-hour.
_CAPABILITY_RELATIVE = 0x40000000

#: ``Win32_Battery.Chemistry`` codes, decoded only when ``BatteryStaticData``
#: is unreadable. 1 (Other) and 2 (Unknown) are deliberately absent from the
#: table: they name no chemistry, and reporting "unknown" is worse than
#: reporting nothing at all.
_WIN32_CHEMISTRY: dict[int, str] = {
    3: "lead-acid",
    4: "nickel-cadmium",
    5: "nickel-metal-hydride",
    6: "lithium-ion",
    7: "zinc-air",
    8: "lithium-polymer",
}

#: Per-pack facts promoted from the selected pack to the top level of
#: :meth:`WmiBatterySource.metadata`.
_PROMOTED_PACK_KEYS = (
    "design_capacity_mwh",
    "full_charged_capacity_mwh",
    "design_voltage_mv",
    "chemistry",
    "device_name",
    "serial",
    "manufacturer",
    "capacity_relative",
)


class WmiBatterySource:
    """Read the laptop/tablet battery through the Windows ``root\\wmi`` classes.

    Requires Windows and the optional ``wmi`` package
    (``pip install "battfeed[wmi]"``).

    Semantics (documented Windows ACPI battery WMI units):

    * ``BatteryStatus.Voltage`` is reported in **millivolt** and is divided
      by 1000 to give ``voltage_volt``.
    * ``BatteryStatus.ChargeRate`` and ``BatteryStatus.DischargeRate`` are
      reported in **milliwatt**. They are combined into ``power_watt`` with
      the BDF sign convention -- positive while charging, negative while
      discharging.
    * ``current_ampere`` is **derived, not measured**: the hardware exposes
      power, so current is computed as ``power_watt / voltage_volt`` (0.0
      when the reported voltage is zero, to avoid dividing by zero). It
      inherits the sign of ``power_watt``.
    * ``state_of_charge_percent`` is ``100 x RemainingCapacity /
      FullChargedCapacity`` (the latter from the ``BatteryFullChargedCapacity``
      class), clamped to 0..100 and omitted whenever either number is missing
      or the full-charge capacity is zero. Like the android source's SoC
      column this sits outside the canonical BDF vocabulary, so strict-BDF
      validation flags it; it is emitted anyway because the pack's own fuel
      gauge is the only capacity reading these classes offer.
    * ``cycle_count`` comes from the ``BatteryCycleCount`` class, which many
      firmwares simply do not implement -- the class is then probed once and
      the column silently omitted for the rest of the run.

    Any of those fields may arrive as the ACPI "unknown" sentinel
    (:data:`UNKNOWN_SENTINEL`, 0x80000000); it is treated as absent rather
    than as a number, so an idle pack never reports a 2.1 GW discharge.

    One sample per installed battery pack is returned on each poll, unless
    ``instance`` pins a single pack.

    Args:
        name: Source label, recorded in metadata and sidecars.
        instance: Which pack to poll. ``None`` (default) polls every
            installed pack, one sample each. An ``int`` selects a pack by
            its position in the ``BatteryStatus`` enumeration, a ``str``
            matches its ``InstanceName`` case-insensitively; both come from
            ``battfeed discover --source wmi``. A selector that matches
            nothing raises ``ValueError`` at construction, provided the
            enumeration itself is readable.
    """

    def __init__(self, name: str = "wmi", instance: int | str | None = None) -> None:
        try:
            import wmi
        except ImportError as exc:
            raise ImportError(
                "WmiBatterySource needs the optional 'wmi' package, which is "
                'not installed. Install it with: pip install "battfeed[wmi]" '
                "(Windows only)."
            ) from exc
        self.name = name
        self._instance = instance
        # COM handles belong to the thread that created them, so every
        # connection is thread-local: a harvester loop running on its own
        # thread makes its own rather than borrowing the constructor's.
        self._local = threading.local()
        self._local.root = wmi.WMI(namespace=WMI_NAMESPACE)
        self._cimv2_unavailable = False
        self._no_cycle_count_class = False
        if instance is not None:
            self._validate_instance()

    @property
    def _connection(self) -> Any:
        """The ``root\\wmi`` handle for the calling thread, made on demand."""
        conn = getattr(self._local, "root", None)
        if conn is None:
            import wmi

            _co_initialize()
            conn = wmi.WMI(namespace=WMI_NAMESPACE)
            self._local.root = conn
        return conn

    @classmethod
    def availability(cls) -> str | None:
        """Return None if this source can run here, else a human-readable reason.

        Used by the CLI ``sources`` listing; does not touch hardware.
        """
        if sys.platform != "win32":
            return "requires Windows"
        if importlib.util.find_spec("wmi") is None:
            return 'missing optional dependency; pip install "battfeed[wmi]"'
        return None

    @classmethod
    def discover(cls, timeout_s: float = 6.0) -> list[dict[str, Any]]:
        """List the battery packs installed in this machine.

        Enumeration is a local WMI query and returns immediately, so
        ``timeout_s`` is accepted for the discovery-hook signature and never
        used. Each candidate's ``option``/``value`` pair pins one pack
        (``--opt instance=0``); collecting without that option polls every
        pack at once, which is what a single-battery laptop wants anyway.
        Descriptive fields are best-effort -- a machine whose
        ``BatteryStaticData`` is unreadable still lists its packs, just with
        fewer details.
        """
        source = cls()
        return [
            {
                "option": "instance",
                "value": index,
                "instance_name": pack.get("instance_name"),
                "device_name": pack.get("device_name"),
                "manufacturer": pack.get("manufacturer"),
                "chemistry": pack.get("chemistry"),
                "design_capacity_mwh": pack.get("design_capacity_mwh"),
                "full_charged_capacity_mwh": pack.get("full_charged_capacity_mwh"),
                "cycle_count": pack.get("cycle_count"),
            }
            for index, pack in enumerate(source.describe_packs())
        ]

    def metadata(self) -> Mapping[str, Any]:
        """Describe the machine and its battery pack(s).

        Every static read is best-effort and individually guarded: on the
        firmware where ``BatteryStaticData`` raises a generic COM error (it
        does on plenty of consumer laptops) the class-level fields are simply
        absent and ``Win32_Battery`` fills in what it can. The facts of the
        polled pack are promoted to the top level for convenience; ``packs``
        always lists every installed pack.
        """
        info: dict[str, Any] = {
            "source": self.name,
            "kind": "wmi-battery",
            "namespace": WMI_NAMESPACE,
            "platform": sys.platform,
            "os_host": platform.node(),
            "notes": (
                "voltage from BatteryStatus.Voltage (mV); power from "
                "ChargeRate/DischargeRate (mW); current derived as power/voltage, "
                "not measured. Positive current = charging (BDF convention). "
                "State of charge is RemainingCapacity/FullChargedCapacity; "
                "capacities are milliwatt-hour unless capacity_relative is set. "
                "Fields reported as 0x80000000 (unknown) are omitted rather than "
                "reported as numbers."
            ),
        }
        packs = self.describe_packs()
        info["battery_count"] = len(packs)
        info["packs"] = packs
        if packs:
            selected = packs[self._promoted_index(packs)]
            for key in _PROMOTED_PACK_KEYS:
                value = selected.get(key)
                if value is not None:
                    info[key] = value
            if selected.get("cycle_count") is not None:
                info["cycle_count"] = selected["cycle_count"]
        return info

    def poll(self) -> list[dict[str, float | int]]:
        """Return one sample per selected pack.

        Raises when the ``BatteryStatus`` enumeration itself fails (the
        harvester's ``ErrorPolicy`` retries), and when ``instance`` names a
        pack that is no longer installed. Enrichment classes that fail are
        omitted, not fatal.
        """
        statuses = list(self._connection.BatteryStatus())
        capacities = _lookup_numbers(
            self._connection, "BatteryFullChargedCapacity", "FullChargedCapacity", statuses
        )
        cycle_counts = self._cycle_counts(statuses)
        samples: list[dict[str, float | int]] = []
        for index in self._selected_indices(statuses):
            status = statuses[index]
            voltage_volt = (_number(_prop(status, "Voltage")) or 0.0) / 1000.0
            charge_mw = _number(_prop(status, "ChargeRate")) or 0.0
            discharge_mw = _number(_prop(status, "DischargeRate")) or 0.0
            power_watt = (charge_mw - discharge_mw) / 1000.0
            current_ampere = power_watt / voltage_volt if voltage_volt > 0 else 0.0
            sample: dict[str, float | int] = {
                "voltage_volt": voltage_volt,
                "current_ampere": current_ampere,
                "power_watt": power_watt,
            }
            soc = _state_of_charge(_number(_prop(status, "RemainingCapacity")), capacities[index])
            if soc is not None:
                sample["state_of_charge_percent"] = soc
            cycle_count = cycle_counts[index]
            if cycle_count is not None:
                sample["cycle_count"] = int(cycle_count)
            samples.append(sample)
        return samples

    def describe_packs(self) -> list[dict[str, Any]]:
        """Return one best-effort static description per installed pack.

        Never raises: a machine that cannot even enumerate ``BatteryStatus``
        (no battery, WMI wedged) yields an empty list.
        """
        statuses = _query(self._connection, "BatteryStatus")
        if not statuses:
            return []
        capacities = _lookup_numbers(
            self._connection, "BatteryFullChargedCapacity", "FullChargedCapacity", statuses
        )
        cycle_counts = self._cycle_counts(statuses)
        static_rows = _lookup_rows(self._connection, "BatteryStaticData", statuses)
        win32_rows = _lookup_rows(self._cimv2(), "Win32_Battery", statuses)
        packs: list[dict[str, Any]] = []
        for index, status in enumerate(statuses):
            pack: dict[str, Any] = {
                "instance_name": _text(_prop(status, "InstanceName")),
                "tag": _int(_prop(status, "Tag")),
                "full_charged_capacity_mwh": _int(capacities[index]),
                "cycle_count": _int(cycle_counts[index]),
                "static_data_available": static_rows[index] is not None,
            }
            pack.update(_static_fields(static_rows[index]))
            _fill_missing(pack, _win32_fields(win32_rows[index]))
            packs.append(pack)
        return packs

    # -- instance selection ---------------------------------------------------

    def _promoted_index(self, packs: list[dict[str, Any]]) -> int:
        """Which pack's static facts are promoted to the top level of metadata."""
        if self._instance is None:
            return 0
        matches = self._match_indices([pack.get("instance_name") for pack in packs])
        return matches[0] if matches else 0

    def _validate_instance(self) -> None:
        """Fail fast on an ``instance`` that names no installed pack.

        A typo should be a usage error at construction rather than a poll
        that retries five times and dies. When the enumeration itself is
        unreadable the check is skipped: a transient WMI hiccup must not
        make a valid configuration unusable, and :meth:`poll` will surface
        the real problem.
        """
        statuses = _query(self._connection, "BatteryStatus")
        if statuses is None:
            return
        if not self._match_indices(_instance_names(statuses)):
            installed = ", ".join(
                f"{index}={name or '?'}" for index, name in enumerate(_instance_names(statuses))
            )
            raise ValueError(
                f"instance={self._instance!r} matches no installed battery pack "
                f"(installed: {installed or 'none'}). "
                "'battfeed discover --source wmi' lists the packs."
            )

    def _selected_indices(self, statuses: list[Any]) -> list[int]:
        """Indices of the packs to sample, raising when the selector misses."""
        if self._instance is None:
            return list(range(len(statuses)))
        matches = self._match_indices(_instance_names(statuses))
        if not matches:
            raise ValueError(
                f"battery pack instance={self._instance!r} is no longer installed "
                f"({len(statuses)} pack(s) present)"
            )
        return matches

    def _match_indices(self, instance_names: list[str | None]) -> list[int]:
        """Resolve ``instance`` to positions: an index directly, a name by match."""
        if isinstance(self._instance, int) and not isinstance(self._instance, bool):
            return [self._instance] if 0 <= self._instance < len(instance_names) else []
        wanted = str(self._instance).strip().lower()
        return [
            index
            for index, name in enumerate(instance_names)
            if (name or "").strip().lower() == wanted
        ]

    # -- enrichment lookups ---------------------------------------------------

    def _cycle_counts(self, statuses: list[Any]) -> list[float | None]:
        """Per-pack cycle count, probed at most once when the class is absent.

        Firmware either implements ``BatteryCycleCount`` or it does not, so
        the first failure is conclusive and the class is never asked again --
        otherwise every poll would pay for (and log) the same COM error.
        """
        if self._no_cycle_count_class:
            return [None] * len(statuses)
        rows = _query(self._connection, "BatteryCycleCount")
        if rows is None:
            self._no_cycle_count_class = True
            return [None] * len(statuses)
        return [
            _number(_prop(row, "CycleCount")) if row is not None else None
            for row in _align(rows, statuses)
        ]

    def _cimv2(self) -> Any | None:
        """Lazily connect to ``root\\cimv2``, remembering a failure.

        Only needed as a fallback, so the connection is never made on a
        machine whose ``BatteryStaticData`` answers properly on the first
        ``metadata()`` call.
        """
        conn = getattr(self._local, "cimv2", None)
        if conn is None and not self._cimv2_unavailable:
            try:
                import wmi

                _co_initialize()
                conn = wmi.WMI(namespace=CIMV2_NAMESPACE)
                self._local.cimv2 = conn
            except Exception as exc:  # noqa: BLE001 -- any COM failure means "no fallback"
                logger.debug("root\\cimv2 unavailable (%s); Win32_Battery fallback disabled", exc)
                self._cimv2_unavailable = True
        return conn


def _co_initialize() -> None:
    """Initialise COM for the calling thread, ignoring "already done".

    ``wmi`` only calls this for the thread that imports it, so a source polled
    from a worker thread raises ``CoInitialize has not been called`` without
    this. Repeat calls on one thread are harmless (S_FALSE); a thread already
    in a different apartment model raises, and that failure belongs to the
    caller's connection attempt, not here.
    """
    try:
        import pythoncom
    except ImportError:  # pywin32 absent: nothing to initialise
        return
    try:
        pythoncom.CoInitialize()
    except Exception as exc:  # noqa: BLE001 -- an apartment clash is the caller's problem
        logger.debug("CoInitialize declined on this thread (%s)", exc)


def _static_fields(row: Any | None) -> dict[str, Any]:
    """Decode ``BatteryStaticData``, one guarded read per property.

    A row that is present can still have individual properties blow up, so
    every read goes through :func:`_prop`; missing values are omitted rather
    than reported as null-ish numbers.
    """
    if row is None:
        return {}
    fields = {
        "design_capacity_mwh": _int(_prop(row, "DesignedCapacity")),
        "design_voltage_mv": _int(_prop(row, "DesignedVoltage")),
        "chemistry": _chemistry(_prop(row, "Chemistry")),
        "device_name": _text(_prop(row, "DeviceName")),
        "serial": _text(_prop(row, "SerialNumber")),
        "manufacturer": _text(_prop(row, "ManufactureName")),
        "capacity_relative": _capacity_relative(_prop(row, "Capabilities")),
    }
    return {key: value for key, value in fields.items() if value is not None}


def _win32_fields(row: Any | None) -> dict[str, Any]:
    """Decode the ``Win32_Battery`` fallback (a subset of the static data).

    ``DesignCapacity``/``FullChargeCapacity`` are null on most consumer
    hardware; they are read anyway because they cost nothing when present.
    There is no manufacturer or serial here -- those stay absent when
    ``BatteryStaticData`` fails. ``DesignVoltage`` is documented as the
    pack's design voltage, but some firmware puts the instantaneous terminal
    voltage there instead, so treat it as an indication rather than a
    nameplate figure.
    """
    if row is None:
        return {}
    fields = {
        "design_capacity_mwh": _int(_prop(row, "DesignCapacity")),
        "full_charged_capacity_mwh": _int(_prop(row, "FullChargeCapacity")),
        "design_voltage_mv": _int(_prop(row, "DesignVoltage")),
        "device_name": _text(_prop(row, "Name")),
        "chemistry": _WIN32_CHEMISTRY.get(_int(_prop(row, "Chemistry")) or 0),
    }
    return {key: value for key, value in fields.items() if value is not None}


def _fill_missing(target: dict[str, Any], extra: Mapping[str, Any]) -> None:
    """Add only the keys ``target`` is still missing (fallbacks never win)."""
    for key, value in extra.items():
        if target.get(key) is None:
            target[key] = value


def _query(connection: Any | None, class_name: str) -> list[Any] | None:
    """Enumerate a WMI class, or None when it is unreadable.

    ``BatteryStaticData`` raises a generic COM error on some firmware and
    ``BatteryCycleCount`` does not exist at all on others, so "the class
    failed" has to be an ordinary, non-fatal outcome for everything except
    ``BatteryStatus``.
    """
    if connection is None:
        return None
    try:
        return list(getattr(connection, class_name)())
    except Exception as exc:  # noqa: BLE001 -- COM raises whatever it likes
        logger.debug("WMI class %s unavailable: %s", class_name, exc)
        return None


def _lookup_rows(connection: Any | None, class_name: str, statuses: list[Any]) -> list[Any | None]:
    """Enumerate a class and line its rows up with the ``BatteryStatus`` rows."""
    return _align(_query(connection, class_name) or [], statuses)


def _lookup_numbers(
    connection: Any | None, class_name: str, attr: str, statuses: list[Any]
) -> list[float | None]:
    """One numeric value per pack, read from a sibling class."""
    return [
        _number(_prop(row, attr)) if row is not None else None
        for row in _lookup_rows(connection, class_name, statuses)
    ]


def _align(rows: list[Any], statuses: list[Any]) -> list[Any | None]:
    """Match a sibling class's rows to the packs of a ``BatteryStatus`` list.

    Matching is by ``InstanceName`` (or ``Tag``), which is how the
    ``root\\wmi`` battery classes identify a pack. ``Win32_Battery`` lives in
    another namespace and shares no such key, so it -- and any class whose
    rows come back keyless -- falls back to positional matching, and only
    when the row counts agree: a two-pack machine reporting a single row of
    static data must not have it attributed to both packs.
    """
    if not rows:
        return [None] * len(statuses)
    by_key: dict[str, Any] = {}
    for row in rows:
        key = _instance_key(row)
        if key is not None:
            by_key[key] = row
    aligned: list[Any | None] = []
    for index, status in enumerate(statuses):
        key = _instance_key(status)
        if key is not None and key in by_key:
            aligned.append(by_key[key])
        elif len(rows) == len(statuses):
            aligned.append(rows[index])
        else:
            aligned.append(None)
    return aligned


def _instance_names(statuses: list[Any]) -> list[str | None]:
    """The ``InstanceName`` of each pack, in enumeration order."""
    return [_text(_prop(status, "InstanceName")) for status in statuses]


def _prop(obj: Any, name: str) -> Any:
    """Read one WMI property, tolerating a per-property COM failure."""
    try:
        return getattr(obj, name, None)
    except Exception as exc:  # noqa: BLE001 -- COM raises whatever it likes
        logger.debug("WMI property %s unreadable: %s", name, exc)
        return None


def _instance_key(obj: Any) -> str | None:
    """Identify the pack a ``root\\wmi`` row belongs to, or None if keyless."""
    name = _text(_prop(obj, "InstanceName"))
    if name:
        return name.lower()
    tag = _prop(obj, "Tag")
    return f"tag:{tag}" if tag is not None else None


def _number(value: Any) -> float | None:
    """Coerce a WMI field to float, or None when missing or "unknown".

    Both the unsigned sentinel (0x80000000) and its signed reading
    (-2147483648) mean "the pack does not know", never a measurement.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if abs(number) == float(UNKNOWN_SENTINEL):
        return None
    return number


def _int(value: Any) -> int | None:
    """Like :func:`_number`, for the integer registers (capacity, voltage)."""
    number = _number(value)
    return None if number is None else int(number)


def _text(value: Any) -> str | None:
    """Strip a WMI string field, mapping blanks to None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _chemistry(value: Any) -> str | None:
    """Decode ``BatteryStaticData.Chemistry``, a 4-character ACPI code.

    The MOF declares the field as ``uint8[4]`` and python-wmi hands it back
    differently depending on the machine: an already-decoded string, a tuple
    of byte values, or -- as on the HP laptop this was developed against --
    one packed integer (1852787020 is ``b"LIon"`` read little-endian). All
    three shapes decode to the same code; anything that is not printable
    ASCII is reported as unreadable rather than as a bare number.
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return _ascii_code(bytes(value))
    if isinstance(value, (list, tuple)):
        try:
            return _ascii_code(bytes(int(item) & 0xFF for item in value))
        except (TypeError, ValueError):
            return None
    if isinstance(value, int) and not isinstance(value, bool):
        packed = (value & 0xFFFFFFFF).to_bytes(4, "little")
        return _ascii_code(packed) or _ascii_code(packed[::-1])
    return _text(value)


def _ascii_code(raw: bytes) -> str | None:
    """A printable chemistry code from four raw bytes, or None if they are not one."""
    text = raw.decode("ascii", "ignore").strip(" \x00")
    return text if len(text) > 1 and text.isalnum() else None


def _capacity_relative(value: Any) -> bool | None:
    """True when the pack declares relative (unitless) capacities.

    ACPI packs may report capacity as an abstract number instead of
    milliwatt-hour, which would make the ``_mwh`` field names a lie. The flag
    is only reported when it is set, so the common mWh case adds no noise.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return True if value & _CAPABILITY_RELATIVE else None


def _state_of_charge(remaining: float | None, full_charged: float | None) -> float | None:
    """Percent of the pack's present full-charge capacity, or None.

    Guarded against every way this division goes wrong in the field: either
    reading absent or reported as the unknown sentinel, a full-charge
    capacity of zero on a pack whose gauge has not learned yet, and a
    remaining capacity that exceeds it (freshly charged packs do report
    slightly over 100%).
    """
    if remaining is None or full_charged is None or full_charged <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * remaining / full_charged))
