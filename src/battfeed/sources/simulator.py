"""Deterministic synthetic coin-cell discharge, for demos and tests."""

from __future__ import annotations

import math
from typing import Any, Mapping

__all__ = ["SimulatedCellSource"]


class SimulatedCellSource:
    """Simulate a CR2032-ish coin cell under constant-current discharge.

    Every :meth:`poll` returns exactly one sample computed as a **pure
    function of the poll count** -- no wall clock, no randomness -- so two
    instances constructed with the same parameters always produce identical
    streams and tests are fully deterministic.

    The synthetic profile:

    * ``voltage_volt`` declines from 3.0 V to 2.0 V with a slight curvature
      (flat plateau, then a knee near end of discharge), reached after
      ``steps_to_empty`` polls and clamped there afterwards.
    * ``current_ampere`` is a constant discharge current. Following the BDF
      sign convention (positive current charges the test object), discharge
      is **negative**: the default is -2 mA.
    * ``surface_temperature_celsius`` idles around 25 degC with a small
      deterministic ripple.

    The source deliberately does not emit ``test_time_second``; the
    harvester stamps elapsed time, exactly as it would for real hardware
    without its own timebase.
    """

    def __init__(
        self,
        name: str = "simulator",
        *,
        steps_to_empty: int = 3600,
        discharge_current_a: float = 0.002,
        full_voltage_v: float = 3.0,
        empty_voltage_v: float = 2.0,
        ambient_c: float = 25.0,
    ) -> None:
        if steps_to_empty <= 0:
            raise ValueError("steps_to_empty must be positive")
        self.name = name
        self._steps_to_empty = steps_to_empty
        self._discharge_current_a = abs(discharge_current_a)
        self._full_v = full_voltage_v
        self._empty_v = empty_voltage_v
        self._ambient_c = ambient_c
        self._polls = 0

    def metadata(self) -> Mapping[str, Any]:
        return {
            "source": self.name,
            "kind": "simulated",
            "cell": "CR2032-like coin cell (synthetic)",
            "chemistry": "Li-MnO2 (synthetic)",
            "full_voltage_volt": self._full_v,
            "empty_voltage_volt": self._empty_v,
            "discharge_current_ampere": -self._discharge_current_a,
            "steps_to_empty": self._steps_to_empty,
            "sign_convention": "positive current charges the cell (BDF)",
        }

    def sample_at(self, step: int) -> dict[str, float]:
        """Return the sample for poll number ``step`` (0-based). Pure function."""
        x = min(step / self._steps_to_empty, 1.0)  # depth of discharge, 0..1
        voltage = self._empty_v + (self._full_v - self._empty_v) * (1.0 - x**2)
        temperature = self._ambient_c + 0.5 * math.sin(step / 30.0)
        return {
            "voltage_volt": round(voltage, 6),
            "current_ampere": -self._discharge_current_a,
            "surface_temperature_celsius": round(temperature, 6),
        }

    def poll(self) -> list[dict[str, float]]:
        sample = self.sample_at(self._polls)
        self._polls += 1
        return [sample]
