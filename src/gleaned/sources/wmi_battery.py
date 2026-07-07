"""Poll the battery of the local Windows machine via WMI."""

from __future__ import annotations

import importlib.util
import sys
from typing import Any, Mapping

__all__ = ["WmiBatterySource"]


class WmiBatterySource:
    """Read the laptop/tablet battery through the Windows ``root\\wmi`` classes.

    Requires Windows and the optional ``wmi`` package
    (``pip install "gleaned[wmi]"``).

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

    One sample per installed battery pack is returned on each poll.
    """

    def __init__(self, name: str = "wmi") -> None:
        try:
            import wmi
        except ImportError as exc:
            raise ImportError(
                "WmiBatterySource needs the optional 'wmi' package, which is "
                'not installed. Install it with: pip install "gleaned[wmi]" '
                "(Windows only)."
            ) from exc
        self.name = name
        self._connection = wmi.WMI(namespace="root\\wmi")

    @classmethod
    def availability(cls) -> str | None:
        """Return None if this source can run here, else a human-readable reason.

        Used by the CLI ``sources`` listing; does not touch hardware.
        """
        if sys.platform != "win32":
            return "requires Windows"
        if importlib.util.find_spec("wmi") is None:
            return 'missing optional dependency; pip install "gleaned[wmi]"'
        return None

    def metadata(self) -> Mapping[str, Any]:
        return {
            "source": self.name,
            "kind": "wmi-battery",
            "namespace": "root\\wmi",
            "platform": sys.platform,
            "notes": (
                "voltage from BatteryStatus.Voltage (mV); power from "
                "ChargeRate/DischargeRate (mW); current derived as power/voltage, "
                "not measured. Positive current = charging (BDF convention)."
            ),
        }

    def poll(self) -> list[dict[str, float]]:
        samples: list[dict[str, float]] = []
        for status in self._connection.BatteryStatus():
            voltage_volt = float(status.Voltage or 0) / 1000.0
            charge_mw = float(status.ChargeRate or 0)
            discharge_mw = float(status.DischargeRate or 0)
            power_watt = (charge_mw - discharge_mw) / 1000.0
            current_ampere = power_watt / voltage_volt if voltage_volt > 0 else 0.0
            samples.append(
                {
                    "voltage_volt": voltage_volt,
                    "current_ampere": current_ampere,
                    "power_watt": power_watt,
                }
            )
        return samples
