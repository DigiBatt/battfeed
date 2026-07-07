"""Poll an Android phone or tablet battery over ADB."""

from __future__ import annotations

import logging
import shutil
from typing import Any, Mapping

from .adb import (
    ADBCommandError,
    ADBDeviceDisconnected,
    ADBError,
    AdbBackend,
    AdbDevice,
    SubprocessAdbBackend,
)
from .parser import (
    battery_health,
    battery_status,
    normalize_current_a,
    normalize_temperature_c,
    normalize_voltage_v,
    parse_dumpsys_battery,
    parse_sysfs_listing,
)

__all__ = ["AndroidBatterySource"]

logger = logging.getLogger(__name__)

SYSFS_BATTERY_PATH = "/sys/class/power_supply/battery/"

#: sysfs power_supply fields read on each poll -- only the ones that feed
#: the emitted columns (voltage, current, temperature, charge status).
_POLL_SYSFS_FIELDS = ("voltage_now", "current_now", "current_avg", "temp", "status")


class AndroidBatterySource:
    """Read an Android device battery through ``adb shell`` (dumpsys + sysfs).

    Requires the Android platform-tools ``adb`` executable on PATH (or an
    explicit ``adb_path``) and a device with USB debugging authorized.
    Each :meth:`poll` returns one sample built from ``dumpsys battery``,
    enriched with kernel ``/sys/class/power_supply/battery/`` fields when
    ``use_sysfs`` is enabled; sysfs values win and dumpsys is the fallback
    (some vendors SELinux-block sysfs for the shell user).

    Emitted columns and conversions:

    * ``voltage_volt`` -- dumpsys ``voltage`` is millivolt, sysfs
      ``voltage_now`` is typically microvolt; both are normalized to volt
      by magnitude.
    * ``current_ampere`` -- magnitude from sysfs ``current_now`` (falling
      back to ``current_avg``, then to dumpsys), normalized from
      microamp/milliamp to ampere. **Sign rule:** BDF defines positive
      current as charging and negative as discharging, but the sign of
      Android's ``current_now`` varies by vendor, so the reported charge
      status forces the sign: ``Charging`` -> +magnitude, ``Discharging``
      -> -magnitude, and any other status (``Full``, ``Not charging``,
      ``Unknown``, missing) keeps the value as reported.
    * ``surface_temperature_celsius`` -- dumpsys ``temperature`` and sysfs
      ``temp`` are tenths of a degree Celsius, converted to degC.
    * ``charge_status`` -- the status string itself (e.g. ``"Charging"``).
      This is a non-vocabulary string column, permitted by the gleaned
      sample contract because status changes over time and is analytically
      valuable; note that strict BDF validation flags columns outside the
      canonical vocabulary.

    Per-poll extras such as level percent, health and plugged source are
    deliberately NOT emitted as columns (they are not in the BDF
    vocabulary); slow-changing facts live in :meth:`metadata` instead.

    Resilience: :meth:`poll` RAISES on adb failures and device
    disconnects. The retry/reconnect machinery of the original standalone
    collector is intentionally not ported -- the gleaned harvester's
    ``ErrorPolicy`` already retries failed polls with exponential backoff,
    so this source stays a thin, raise-on-trouble reader.

    Args:
        serial: ADB serial of the device to poll. ``None`` targets the
            single connected device and errors clearly when several are
            attached.
        adb_path: The ``adb`` executable to invoke (default: from PATH).
        use_sysfs: Also read kernel power_supply fields on each poll.
        backend: Injectable :class:`AdbBackend` for tests; defaults to a
            :class:`SubprocessAdbBackend` running ``adb_path``.
    """

    def __init__(
        self,
        serial: str | None = None,
        adb_path: str = "adb",
        use_sysfs: bool = True,
        backend: AdbBackend | None = None,
    ) -> None:
        self.name = "android"
        self._serial = serial
        self._adb_path = adb_path
        self._use_sysfs = use_sysfs
        self._backend: AdbBackend = backend or SubprocessAdbBackend(adb_path)
        self._metadata_cache: dict[str, Any] | None = None

    @classmethod
    def availability(cls) -> str | None:
        """Return None if this source can run here, else a human-readable reason.

        Used by the CLI ``sources`` listing; does not touch hardware.
        """
        if shutil.which("adb") is None:
            return 'requires the Android platform-tools "adb" executable on PATH'
        return None

    def metadata(self) -> Mapping[str, Any]:
        """Describe the polled device: identity, battery technology, health.

        Probed over adb (``getprop`` + ``dumpsys battery``) and cached
        after the first successful fetch. Probe failures are tolerated:
        the static fields are still returned, device fields stay ``None``,
        and nothing is cached so the next call retries.
        """
        if self._metadata_cache is not None:
            return self._metadata_cache
        info: dict[str, Any] = {
            "source": self.name,
            "kind": "android-adb-battery",
            "adb_path": self._adb_path,
            "adb_serial": self._serial,
            "manufacturer": None,
            "model": None,
            "android_version": None,
            "serial": None,
            "battery_technology": None,
            "battery_health": None,
            "use_sysfs": self._use_sysfs,
            "sysfs_available": None,
            "notes": (
                "voltage from sysfs voltage_now (uV) or dumpsys voltage (mV); "
                "current magnitude from sysfs/dumpsys current_now with its sign "
                "forced by charge status (positive = charging, BDF convention); "
                "temperature reported in tenths of degC."
            ),
        }
        try:
            device = self._resolve_device()
            info["adb_serial"] = device.serial
            info["manufacturer"] = self._getprop(device, "ro.product.manufacturer")
            info["model"] = self._getprop(device, "ro.product.model")
            info["android_version"] = self._getprop(device, "ro.build.version.release")
            info["serial"] = self._getprop(device, "ro.serialno") or device.serial
            dumpsys = parse_dumpsys_battery(self._backend.shell(device, ["dumpsys", "battery"]))
            technology = dumpsys.get("technology")
            info["battery_technology"] = str(technology) if technology is not None else None
            info["battery_health"] = battery_health(dumpsys.get("health"))
            if self._use_sysfs:
                info["sysfs_available"] = self._sysfs_listing(device) is not None
        except ADBError as exc:
            logger.warning("Android metadata probe failed (returning partial): %s", exc)
            return info  # not cached; retried on the next call
        self._metadata_cache = info
        return info

    def poll(self) -> list[dict[str, float | int | str]]:
        """Return one sample from ``dumpsys battery`` (+ sysfs enrichment).

        Raises on adb failures and disconnects; see the class docstring
        for the harvester-side retry story and the current sign rule.
        """
        device = self._resolve_device()
        dumpsys = parse_dumpsys_battery(self._backend.shell(device, ["dumpsys", "battery"]))
        sysfs = self._read_sysfs(device) if self._use_sysfs else {}

        status = _first_text(battery_status(dumpsys.get("status")), sysfs.get("status"))

        current_a = normalize_current_a(sysfs.get("current_now"))
        if current_a is None:
            current_a = normalize_current_a(sysfs.get("current_avg"))
        if current_a is None:
            # Samsung exposes "current now" in dumpsys even when sysfs is
            # SELinux-blocked for the shell user (as on the Galaxy Tab A11 / SM-X230).
            current_a = normalize_current_a(dumpsys.get("current_now"))
        current_a = _signed_current(current_a, status)

        voltage_v = normalize_voltage_v(sysfs.get("voltage_now"))
        if voltage_v is None:
            voltage_v = normalize_voltage_v(dumpsys.get("voltage"))

        temperature_c = normalize_temperature_c(sysfs.get("temp"))
        if temperature_c is None:
            temperature_c = normalize_temperature_c(dumpsys.get("temperature"))

        sample: dict[str, float | int | str] = {}
        if voltage_v is not None:
            sample["voltage_volt"] = voltage_v
        if current_a is not None:
            sample["current_ampere"] = current_a
        if temperature_c is not None:
            sample["surface_temperature_celsius"] = temperature_c
        if status is not None:
            sample["charge_status"] = status
        return [sample] if sample else []

    def close(self) -> None:
        """Release resources. Idempotent; the subprocess backend holds none."""

    def _resolve_device(self) -> AdbDevice:
        """Pick the device to poll, raising clearly when that is impossible."""
        devices = self._backend.list_devices()
        if self._serial is not None:
            for device in devices:
                if device.serial == self._serial:
                    if device.state != "device":
                        raise ADBDeviceDisconnected(
                            f"Android device {self._serial} is not ready (state: {device.state})"
                        )
                    return device
            raise ADBDeviceDisconnected(f"Android device {self._serial} is not connected")
        ready = [device for device in devices if device.state == "device"]
        if not ready:
            states = ", ".join(f"{d.serial}={d.state}" for d in devices) or "none"
            raise ADBDeviceDisconnected(f"no ready Android device (adb devices: {states})")
        if len(ready) > 1:
            serials = ", ".join(sorted(device.serial for device in ready))
            raise ADBError(
                f"{len(ready)} Android devices connected ({serials}); pass serial=... to pick one"
            )
        return ready[0]

    def _read_sysfs(self, device: AdbDevice) -> dict[str, str]:
        """Read the useful power_supply fields, tolerating per-field failures.

        A missing or SELinux-blocked sysfs tree degrades to ``{}`` (dumpsys
        remains as fallback); a device disconnect propagates so the whole
        poll fails and the harvester retries.
        """
        listing = self._sysfs_listing(device)
        if listing is None:
            return {}
        fields: dict[str, str] = {}
        for name in _POLL_SYSFS_FIELDS:
            if name not in listing:
                continue
            try:
                fields[name] = self._backend.shell(
                    device, ["cat", f"{SYSFS_BATTERY_PATH}{name}"]
                ).strip()
            except ADBDeviceDisconnected:
                raise
            except ADBCommandError as exc:
                logger.debug("sysfs field %s unavailable for %s: %s", name, device.serial, exc)
        return fields

    def _sysfs_listing(self, device: AdbDevice) -> set[str] | None:
        """List the sysfs battery directory, or None when it is unreadable."""
        try:
            return parse_sysfs_listing(self._backend.shell(device, ["ls", SYSFS_BATTERY_PATH]))
        except ADBDeviceDisconnected:
            raise
        except ADBCommandError as exc:
            logger.debug("sysfs listing unavailable for %s: %s", device.serial, exc)
            return None

    def _getprop(self, device: AdbDevice, name: str) -> str | None:
        value = self._backend.shell(device, ["getprop", name]).strip()
        return value or None


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _signed_current(current_a: float | None, status: str | None) -> float | None:
    """Force the BDF sign convention onto a vendor-signed current reading.

    Positive current charges the battery, negative discharges it. Exact
    status matching is deliberate: ``"Not charging"`` must not trip the
    charging branch, so substring checks are avoided.
    """
    if current_a is None:
        return None
    low = (status or "").strip().lower()
    if low == "charging":
        return abs(current_a)
    if low == "discharging":
        return -abs(current_a)
    return current_a  # Full / Not charging / Unknown: keep the reported sign
