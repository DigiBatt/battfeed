"""Parsing and unit normalization helpers for Android battery telemetry.

Covers the two places Android exposes battery state to ``adb shell``:

* ``dumpsys battery`` -- key/value lines from the framework battery
  service (millivolt voltage, tenths-of-degC temperature, enum codes for
  status/health, plugged-source booleans).
* ``/sys/class/power_supply/battery/`` -- the kernel's power_supply
  class (microvolt/microamp by specification, though vendor builds vary,
  hence the magnitude heuristics in the ``normalize_*`` helpers).
"""

from __future__ import annotations

import re
from typing import Any

from .adb import AdbDevice

__all__ = [
    "BATTERY_HEALTH",
    "BATTERY_STATUS",
    "battery_health",
    "battery_status",
    "dumpsys_soc_pct",
    "normalize_capacity_pct",
    "normalize_charge_ah",
    "normalize_current_a",
    "normalize_energy_wh",
    "normalize_temperature_c",
    "normalize_voltage_v",
    "parse_adb_devices",
    "parse_dumpsys_battery",
    "parse_mdns_services",
    "parse_sysfs_listing",
    "plugged_source",
]

_KEY_RE = re.compile(r"[^a-z0-9]+")

#: android.os.BatteryManager BATTERY_STATUS_* codes as reported by dumpsys.
BATTERY_STATUS = {
    1: "Unknown",
    2: "Charging",
    3: "Discharging",
    4: "Not charging",
    5: "Full",
}

#: android.os.BatteryManager BATTERY_HEALTH_* codes as reported by dumpsys.
BATTERY_HEALTH = {
    1: "Unknown",
    2: "Good",
    3: "Overheat",
    4: "Dead",
    5: "Over voltage",
    6: "Unspecified failure",
    7: "Cold",
}


def parse_adb_devices(text: str) -> list[AdbDevice]:
    """Parse ``adb devices -l`` output."""
    devices: list[AdbDevice] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("List of devices"):
            continue
        if line.startswith("* ") or line.startswith("adb server"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        qualifiers: dict[str, str] = {}
        for token in parts[2:]:
            if ":" in token:
                key, value = token.split(":", 1)
                qualifiers[key] = value
        devices.append(
            AdbDevice(
                serial=parts[0],
                state=parts[1],
                transport_id=qualifiers.get("transport_id"),
                qualifiers=qualifiers,
            )
        )
    return devices


def parse_mdns_services(text: str) -> list[tuple[str, str]]:
    """Parse ``adb mdns services`` output into ``(instance, host:port)`` pairs.

    Only connectable listeners are returned (``_adb-tls-connect._tcp`` and the
    legacy ``_adb._tcp``); pairing services (``_adb-tls-pairing._tcp``) are
    deliberately excluded -- they are not ``adb connect`` targets. Rows whose
    last column is not a ``host:port`` are skipped.
    """
    pairs: list[tuple[str, str]] = []
    for raw in text.splitlines():
        parts = raw.split()
        if len(parts) < 3:
            continue
        instance, regtype, address = parts[0], parts[1], parts[-1]
        if not (regtype.startswith("_adb-tls-connect._tcp") or regtype.startswith("_adb._tcp")):
            continue
        host, sep, port = address.rpartition(":")
        if not sep or not host or not port.isdigit():
            continue
        pairs.append((instance, address))
    return pairs


def parse_dumpsys_battery(text: str) -> dict[str, Any]:
    """Parse the key/value lines from ``adb shell dumpsys battery``."""
    fields: dict[str, Any] = {}
    for raw in text.splitlines():
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        normalized = _normalize_key(key)
        if not normalized:
            continue
        fields[normalized] = _parse_value(value.strip())
    return fields


def parse_sysfs_listing(text: str) -> set[str]:
    """Parse ``ls /sys/class/power_supply/battery/`` output."""
    return {part.strip() for part in text.split() if part.strip()}


def battery_status(value: Any) -> str | None:
    """Map a dumpsys status code (or passthrough string) to its name."""
    if value is None:
        return None
    n = _as_int(value)
    if n is not None:
        return BATTERY_STATUS.get(n, str(n))
    return str(value)


def battery_health(value: Any) -> str | None:
    """Map a dumpsys health code (or passthrough string) to its name."""
    if value is None:
        return None
    n = _as_int(value)
    if n is not None:
        return BATTERY_HEALTH.get(n, str(n))
    return str(value)


def plugged_source(fields: dict[str, Any]) -> str:
    """Return the best plugged source from parsed dumpsys booleans."""
    if fields.get("ac_powered") is True:
        return "AC"
    if fields.get("usb_powered") is True:
        return "USB"
    if fields.get("wireless_powered") is True:
        return "wireless"
    if fields.get("dock_powered") is True:
        return "dock"
    return "none"


def normalize_voltage_v(value: Any) -> float | None:
    """Normalize Android voltage values to volts.

    Kernel sysfs usually reports microvolts, while ``dumpsys battery`` usually
    reports millivolts. The magnitude distinguishes the common cases.
    """
    n = _as_float(value)
    if n is None:
        return None
    magnitude = abs(n)
    if magnitude >= 100_000:
        return n / 1_000_000.0
    if magnitude >= 100:
        return n / 1_000.0
    return n


def normalize_current_a(value: Any) -> float | None:
    """Normalize Android current values to amps.

    ``current_now`` is specified as microamps by the Linux power_supply class.
    Some vendor builds expose milliamps for small magnitudes, so a conservative
    magnitude heuristic is used for human-scale values.
    """
    n = _as_float(value)
    if n is None:
        return None
    magnitude = abs(n)
    if magnitude >= 10_000:
        return n / 1_000_000.0
    if magnitude >= 100:
        return n / 1_000.0
    return n


def normalize_temperature_c(value: Any) -> float | None:
    """Normalize Android battery temperature to Celsius.

    Both dumpsys and sysfs report tenths of a degree Celsius.
    """
    n = _as_float(value)
    if n is None:
        return None
    return n / 10.0 if abs(n) > 100 else n


def normalize_capacity_pct(value: Any) -> float | None:
    """Clamp a state-of-charge reading into the 0..100 percent range."""
    n = _as_float(value)
    if n is None:
        return None
    return max(0.0, min(100.0, n))


def normalize_charge_ah(value: Any) -> float | None:
    """Normalize charge-like sysfs fields to amp-hours where possible."""
    n = _as_float(value)
    if n is None:
        return None
    magnitude = abs(n)
    if magnitude >= 10_000:
        return n / 1_000_000.0
    if magnitude >= 100:
        return n / 1_000.0
    return n


def normalize_energy_wh(value: Any) -> float | None:
    """Normalize energy-like sysfs fields to watt-hours where possible."""
    n = _as_float(value)
    if n is None:
        return None
    magnitude = abs(n)
    if magnitude >= 10_000:
        return n / 1_000_000.0
    if magnitude >= 100:
        return n / 1_000.0
    return n


def dumpsys_soc_pct(fields: dict[str, Any]) -> float | None:
    """State of charge in percent from dumpsys ``level`` and ``scale``."""
    level = _as_float(fields.get("level"))
    scale = _as_float(fields.get("scale"))
    if level is None:
        return None
    if scale and scale != 100:
        return 100.0 * level / scale
    return normalize_capacity_pct(level)


def _normalize_key(value: str) -> str:
    return _KEY_RE.sub("_", value.strip().lower()).strip("_")


def _parse_value(value: str) -> Any:
    low = value.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    n = _as_int(value)
    if n is not None:
        return n
    f = _as_float(value)
    return f if f is not None else value


def _as_int(value: Any) -> int | None:
    try:
        if isinstance(value, bool):
            return None
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        if isinstance(value, bool):
            return None
        text = str(value).strip()
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None
