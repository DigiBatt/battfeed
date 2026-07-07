"""SkyRC MC3000 wire protocol -- frame builders and decoders.

This module is the single source of truth for the MC3000 byte protocol. It is
transport-agnostic: BLE, USB and the mock transport all speak the same frames.

Provenance of the layout
------------------------
The real-time slot-readout layout below is taken from the *official SkyRC MC3000
Android app*, decompiled and documented by the community
(https://github.com/kolinger/skyrc-mc3000, ``mc3000ble.py``). That is the most
authoritative source available. An older reverse-engineering of the Windows
updater exists (https://github.com/jaypikay/mc3000 + the goatpr0n.farm write-up);
it agrees on the framing and commands but its *field offsets are guessed* and
differ by one byte and on temperature width -- where they disagree we follow the
APK.

Safety
------
This integration is strictly READ-ONLY. Only the read opcodes in
``READ_ONLY_COMMANDS`` may ever be put on the wire. The command builders refuse
to encode any control opcode (start/stop/write-settings); attempting to do so
raises :class:`ReadOnlyViolation`. Control of the charger is deliberately not
implemented.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# --- framing ---------------------------------------------------------------
HEADER = 0x0F  # UART_DATA_START -- first byte of every frame
USB_LENGTH_MARKER = 0x04  # second byte of a USB request frame (0f 04 <cmd> ..)
TAIL = (0xFF, 0xFF)  # USB request frames are terminated with ff ff
FRAME_LEN_USB = 64  # a USB HID report is 64 bytes
FRAME_LEN_BLE = 20  # a BLE (HM-10) notification carries a 20-byte frame

# --- opcodes ---------------------------------------------------------------
# Read opcodes -- the ONLY bytes this integration is permitted to transmit.
CMD_SLOT_PROGRESS = 0x55  # real-time per-slot measurements (V, I, mAh, T, ...)
CMD_SLOT_SETTINGS = 0x5F  # per-slot program configuration (read)
CMD_MACHINE_INFO = 0x5A  # system / machine info (firmware, serial)
READ_ONLY_COMMANDS = frozenset({CMD_SLOT_PROGRESS, CMD_SLOT_SETTINGS, CMD_MACHINE_INFO})

# Control opcodes -- documented so we can explicitly REFUSE them. Never sent.
_CONTROL_COMMANDS = {
    0x05: "start charging",
    0xFE: "stop charging",
    0x11: "write slot settings",
}

SLOT_COUNT = 4  # the MC3000 has four independent channels (slots 0..3)

# --- value maps (from the official app) ------------------------------------
BATTERY_TYPES = {
    0: "LiIon",
    1: "LiFe",
    2: "LiIo4.35",
    3: "NiMH",
    4: "NiCd",
    5: "NiZn",
    6: "Eneloop",
    7: "RAM",
    8: "BatLTO",
}
# Selectable operating modes depend on the battery-type group.
_MODE_GROUPS = {
    0: {0: "Charge", 1: "Refresh", 2: "Storage", 3: "Discharge", 4: "Cycle"},
    1: {0: "Charge", 1: "Refresh", 2: "Discharge", 3: "Cycle"},
    2: {0: "Charge", 1: "Refresh", 2: "Break-in", 3: "Discharge", 4: "Cycle"},
}
_MODE_TYPE_MAPPING = {0: [0, 1, 2, 8], 1: [5, 7], 2: [3, 4, 6]}
STATUSES = {
    0: "Standby",
    1: "Charging",
    2: "Discharging",
    3: "Pause",
    4: "Completed",
    128: "Input low voltage",
    129: "Input high voltage",
    130: "ADC MCP3424-1 error",
    131: "ADC MCP3424-2 error",
    132: "Connection break",
    133: "Check voltage",
    134: "Capacity limit reached",
    135: "Time limit reached",
    136: "SysTemp too hot",
    137: "Battery too hot",
    138: "Short circuit",
    139: "Wrong polarity",
    140: "Bad battery (high IR)",
}
STATUS_CHARGING = 1
STATUS_DISCHARGING = 2
_RESISTANCE_INVALID = {0, 1, 0xFFFF}


class ProtocolError(Exception):
    """Raised when a frame cannot be decoded (bad length or checksum)."""


class ReadOnlyViolation(Exception):
    """Raised on any attempt to build a non-read (control) command frame."""


def checksum(payload: bytes | bytearray | list[int]) -> int:
    """Frame checksum: the low byte of the sum of all preceding bytes.

    Identical on USB and BLE. A frame is valid iff ``checksum(frame[:-1]) ==
    frame[-1]``.
    """
    return sum(payload) & 0xFF


def valid_checksum(frame: bytes) -> bool:
    return len(frame) >= 2 and checksum(frame[:-1]) == frame[-1]


def resolve_mode(battery_type_code: int, mode_code: int) -> str:
    for group, types in _MODE_TYPE_MAPPING.items():
        if battery_type_code in types:
            return _MODE_GROUPS[group].get(mode_code, f"Mode{mode_code}")
    return f"Mode{mode_code}"


def _led_color(led_bits: int, slot: int) -> str:
    if (led_bits >> slot) & 1:
        return "red"
    if (led_bits >> (slot + 4)) & 1:
        return "green"
    return "none"


# --- command builders (read-only) ------------------------------------------
def _guard_read_only(cmd: int) -> None:
    if cmd in READ_ONLY_COMMANDS:
        return
    what = _CONTROL_COMMANDS.get(cmd, "unknown / non-read")
    raise ReadOnlyViolation(
        f"refusing to build command 0x{cmd:02x} ({what}); this integration is "
        f"read-only and may only send {sorted(hex(c) for c in READ_ONLY_COMMANDS)}"
    )


def build_ble_request(cmd: int, slot: int = 0) -> bytes:
    """Build a 20-byte BLE request frame: ``0f <cmd> <slot> 00.. <checksum>``."""
    _guard_read_only(cmd)
    payload = bytearray(FRAME_LEN_BLE)
    payload[0] = HEADER
    payload[1] = cmd
    payload[2] = slot
    payload[-1] = checksum(payload[:-1])
    return bytes(payload)


def build_usb_request(cmd: int, slot: int = 0) -> bytes:
    """Build a 64-byte USB request frame: ``0f 04 <cmd> 00 <slot> <chk> ff ff``.

    The USB framing (the ``0f 04`` prefix and ``ff ff`` tail) follows the
    DataExplorer / jaypikay reference; the response payload is decoded with the
    same APK-derived layout as BLE.
    """
    _guard_read_only(cmd)
    payload = bytearray(FRAME_LEN_USB)
    payload[0] = HEADER
    payload[1] = USB_LENGTH_MARKER
    payload[2] = cmd
    payload[3] = 0x00
    payload[4] = slot
    payload[5] = (cmd + slot) & 0xFF
    payload[6], payload[7] = TAIL
    return bytes(payload)


# --- decoded value object --------------------------------------------------
@dataclass(frozen=True)
class SlotReading:
    """One decoded real-time slot measurement, in raw device units.

    Units are as the device reports them: voltage in mV, current magnitude in
    mA, capacity in mAh, temperature in whole degrees (unit per machine
    settings, Celsius by default), resistance in mOhm. Sign and unit conversion
    to canonical BDF columns happens in :mod:`gleaned.sources.mc3000.source`.
    """

    slot: int
    battery_type_code: int
    mode_code: int
    program_count: int
    status_code: int
    elapsed_s: int
    voltage_mv: int
    current_ma: int
    capacity_mah: int
    temperature: int
    resistance_mohm: int | None
    led_bits: int
    raw_hex: str = field(default="", compare=False)

    # convenience labels
    @property
    def battery_type(self) -> str:
        return BATTERY_TYPES.get(self.battery_type_code, f"Type{self.battery_type_code}")

    @property
    def mode(self) -> str:
        return resolve_mode(self.battery_type_code, self.mode_code)

    @property
    def status(self) -> str:
        return STATUSES.get(self.status_code, "unknown error")

    @property
    def led(self) -> str:
        return _led_color(self.led_bits, self.slot)

    @property
    def occupied(self) -> bool:
        """True when a cell appears to be inserted (non-zero terminal voltage)."""
        return self.voltage_mv > 0

    @property
    def is_charging(self) -> bool:
        return self.status_code == STATUS_CHARGING

    @property
    def is_discharging(self) -> bool:
        return self.status_code == STATUS_DISCHARGING


def decode_progress(frame: bytes) -> SlotReading:
    """Decode a real-time slot-readout frame (response to ``CMD_SLOT_PROGRESS``).

    Works for both the 20-byte BLE frame and the 64-byte USB frame; all fields
    live in the first 18 bytes and the checksum is always the final byte. Raises
    :class:`ProtocolError` on a bad length, header, opcode or checksum.
    """
    if len(frame) < FRAME_LEN_BLE:
        raise ProtocolError(f"frame too short: {len(frame)} bytes")
    if frame[0] != HEADER:
        raise ProtocolError(f"bad header 0x{frame[0]:02x}, expected 0x0f")
    if frame[1] != CMD_SLOT_PROGRESS:
        raise ProtocolError(f"not a progress frame (opcode 0x{frame[1]:02x})")
    if not valid_checksum(frame):
        raise ProtocolError("checksum mismatch")

    resistance = struct.unpack_from(">H", frame, 16)[0]
    return SlotReading(
        slot=frame[2],
        battery_type_code=frame[3],
        mode_code=frame[4],
        program_count=frame[5],
        status_code=frame[6],
        elapsed_s=struct.unpack_from(">H", frame, 7)[0],
        voltage_mv=struct.unpack_from(">H", frame, 9)[0],
        current_ma=struct.unpack_from(">H", frame, 11)[0],
        capacity_mah=struct.unpack_from(">H", frame, 13)[0],
        temperature=frame[15],
        resistance_mohm=None if resistance in _RESISTANCE_INVALID else resistance,
        led_bits=frame[18],
        raw_hex=frame.hex(),
    )


@dataclass(frozen=True)
class MachineInfo:
    """Instrument identity, used to tag the source / provenance."""

    serial: str
    firmware: str | None
    raw_hex: str = field(default="", compare=False)


def decode_machine_info(frame: bytes) -> MachineInfo:
    """Best-effort decode of the machine-info response (``CMD_MACHINE_INFO``).

    Only the serial is decoded robustly: it is the hex of bytes 16..30 (per the
    jaypikay reference). The rest of that frame's layout is not reliably
    documented, so firmware is left unset unless clearly present. Identity only --
    never on the measurement path.
    """
    if len(frame) < 31:
        raise ProtocolError(f"machine-info frame too short: {len(frame)} bytes")
    if frame[0] != HEADER:
        raise ProtocolError(f"bad header 0x{frame[0]:02x}")
    serial = frame[16:31].hex().upper()
    return MachineInfo(serial=serial, firmware=None, raw_hex=frame.hex())


def encode_machine_info(serial: str, frame_len: int = FRAME_LEN_USB) -> bytes:
    """Build a machine-info frame carrying ``serial`` (used by the mock/tests)."""
    b = bytearray(frame_len)
    b[0] = HEADER
    b[1] = CMD_MACHINE_INFO
    raw = bytes.fromhex(serial)[:15] if _is_hex(serial) else serial.encode()[:15]
    b[16 : 16 + len(raw)] = raw
    b[-1] = checksum(b[:-1])
    return bytes(b)


def _is_hex(s: str) -> bool:
    try:
        bytes.fromhex(s)
        return True
    except ValueError:
        return False


def encode_progress(r: SlotReading, frame_len: int = FRAME_LEN_USB) -> bytes:
    """Inverse of :func:`decode_progress` -- build a valid measurement frame.

    Used by the mock transport and the tests so the full pipeline can be
    exercised without hardware and round-trip-verified against the decoder.
    """
    b = bytearray(frame_len)
    b[0] = HEADER
    b[1] = CMD_SLOT_PROGRESS
    b[2] = r.slot & 0xFF
    b[3] = r.battery_type_code & 0xFF
    b[4] = r.mode_code & 0xFF
    b[5] = r.program_count & 0xFF
    b[6] = r.status_code & 0xFF
    struct.pack_into(">H", b, 7, max(0, r.elapsed_s) & 0xFFFF)
    struct.pack_into(">H", b, 9, max(0, r.voltage_mv) & 0xFFFF)
    struct.pack_into(">H", b, 11, max(0, r.current_ma) & 0xFFFF)
    struct.pack_into(">H", b, 13, max(0, r.capacity_mah) & 0xFFFF)
    b[15] = int(r.temperature) & 0xFF
    struct.pack_into(">H", b, 16, (r.resistance_mohm or 0) & 0xFFFF)
    b[18] = r.led_bits & 0xFF
    b[-1] = checksum(b[:-1])
    return bytes(b)
