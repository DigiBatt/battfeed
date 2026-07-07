"""Mock transport -- a simulated MC3000 that speaks the real wire protocol.

It encodes genuine measurement frames with :func:`protocol.encode_progress`, so
the decoder, reader and source all run end-to-end with no hardware, and every
frame round-trips through the exact production decode path.

The simulation is a simple coulomb-counter per slot with a realistic
voltage/SoC/temperature relationship, deterministic (no RNG) so tests are
stable. It advances a slot's state by ``dt_s`` each time that slot is polled.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..protocol import (
    CMD_MACHINE_INFO,
    CMD_SLOT_PROGRESS,
    STATUS_CHARGING,
    STATUS_DISCHARGING,
    SlotReading,
    encode_machine_info,
    encode_progress,
)
from .base import Transport

__all__ = ["MockTransport", "SlotSim"]

# battery-type codes (see protocol.BATTERY_TYPES)
_LIION, _NIMH = 0, 3
# mode codes within the Li / Ni groups
_MODE_CHARGE, _MODE_STORAGE, _MODE_DISCHARGE, _MODE_CYCLE_LI = 0, 2, 3, 4
_STATUS_STANDBY, _STATUS_COMPLETED = 0, 4


@dataclass
class SlotSim:
    """One simulated channel."""

    slot: int
    present: bool
    battery_type_code: int = _LIION
    mode_code: int = _MODE_CHARGE
    capacity_nominal_mah: float = 3000.0
    rate_ma: float = 1000.0
    v_min: float = 3.0
    v_max: float = 4.2
    soc: float = 0.2  # 0..1
    ambient_c: float = 24.0
    # dynamic
    elapsed_s: int = 0
    delivered_mah: float = 0.0  # accumulated capacity this run
    status_code: int = STATUS_CHARGING
    program_count: int = 0
    _direction: int = 1  # +1 charging, -1 discharging (for cycle mode)

    def __post_init__(self) -> None:
        if not self.present:
            self.status_code = _STATUS_STANDBY
        elif self.mode_code == _MODE_DISCHARGE:
            self.status_code, self._direction = STATUS_DISCHARGING, -1
        elif self.mode_code == _MODE_CYCLE_LI:
            self.status_code, self._direction = STATUS_CHARGING, 1

    def advance(self, dt_s: float) -> None:
        if not self.present or self.status_code in (_STATUS_STANDBY, _STATUS_COMPLETED):
            return
        self.elapsed_s += int(dt_s)
        d_ah = self.rate_ma / 1000.0 * dt_s / 3600.0
        d_soc = d_ah / (self.capacity_nominal_mah / 1000.0)
        self.soc += self._direction * d_soc
        self.delivered_mah += self.rate_ma * dt_s / 3600.0

        if self._direction > 0 and self.soc >= 1.0:
            self.soc = 1.0
            self._end_of_leg(charged=True)
        elif self._direction < 0 and self.soc <= 0.0:
            self.soc = 0.0
            self._end_of_leg(charged=False)
        self.status_code = STATUS_CHARGING if self._direction > 0 else STATUS_DISCHARGING

    def _end_of_leg(self, charged: bool) -> None:
        if self.mode_code == _MODE_CYCLE_LI:  # reverse and keep going
            self._direction *= -1
            self.program_count += 1 if charged else 0
            self.delivered_mah = 0.0
        else:
            self.status_code = _STATUS_COMPLETED

    def reading(self) -> SlotReading:
        # Terminal voltage: OCV from SoC plus a small IR polarisation term whose
        # sign follows the current direction (higher while charging).
        ocv = self.v_min + self.soc * (self.v_max - self.v_min)
        active = self.status_code in (STATUS_CHARGING, STATUS_DISCHARGING)
        ir_term = 0.08 * self._direction if active else 0.0
        ripple = 0.004 * math.sin(self.elapsed_s / 30.0) if active else 0.0
        voltage_mv = int(round((ocv + ir_term + ripple) * 1000)) if self.present else 0
        current_ma = int(self.rate_ma) if active else 0
        temp = self.ambient_c + (8.0 * self.soc if active else 0.0)
        return SlotReading(
            slot=self.slot,
            battery_type_code=self.battery_type_code,
            mode_code=self.mode_code,
            program_count=self.program_count,
            status_code=self.status_code,
            elapsed_s=self.elapsed_s,
            voltage_mv=voltage_mv,
            current_ma=current_ma,
            capacity_mah=int(round(self.delivered_mah)),
            temperature=int(round(temp)),
            resistance_mohm=45 if (self.present and active) else None,
            led_bits=(1 << self.slot)
            if self.status_code == STATUS_DISCHARGING
            else (1 << (self.slot + 4))
            if active
            else 0,
        )


def _default_slots() -> list[SlotSim]:
    """A representative mixed load: a charge, a discharge, a cycle, an empty slot."""
    return [
        SlotSim(
            0,
            present=True,
            battery_type_code=_LIION,
            mode_code=_MODE_CHARGE,
            capacity_nominal_mah=3000,
            rate_ma=1000,
            soc=0.15,
        ),
        SlotSim(
            1,
            present=True,
            battery_type_code=_LIION,
            mode_code=_MODE_DISCHARGE,
            capacity_nominal_mah=2500,
            rate_ma=500,
            soc=0.92,
        ),
        SlotSim(
            2,
            present=True,
            battery_type_code=_NIMH,
            mode_code=_MODE_CYCLE_LI,
            capacity_nominal_mah=2000,
            rate_ma=1000,
            v_min=1.0,
            v_max=1.45,
            soc=0.5,
        ),
        SlotSim(3, present=False),
    ]


class MockTransport(Transport):
    frame_kind = "usb"

    def __init__(
        self, dt_s: float = 1.0, serial: str = "MOCKMC3000DEV01", slots: list[SlotSim] | None = None
    ) -> None:
        self.dt_s = dt_s
        self.serial = serial
        self.sims = {s.slot: s for s in (slots or _default_slots())}
        self._open = False

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def poll(self, cmd: int, slot: int) -> bytes:
        if cmd == CMD_MACHINE_INFO:
            return encode_machine_info(self.serial.encode().hex())
        if cmd == CMD_SLOT_PROGRESS:
            sim = self.sims[slot]
            sim.advance(self.dt_s)
            return encode_progress(sim.reading())
        raise ValueError(f"mock: unsupported command 0x{cmd:02x}")
