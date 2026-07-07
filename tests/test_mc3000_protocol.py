"""MC3000 protocol decode/encode + read-only safety tests (no hardware)."""

from __future__ import annotations

import struct

import pytest

from gleaned.sources.mc3000 import protocol as p


def _known_progress_frame() -> bytes:
    """A hand-built frame pinning the APK-derived offsets (slot 2, discharging)."""
    f = bytearray(p.FRAME_LEN_USB)
    f[0] = p.HEADER
    f[1] = p.CMD_SLOT_PROGRESS
    f[2] = 2  # slot
    f[3] = 0  # LiIon
    f[4] = 3  # mode 3 -> Discharge (Li group)
    f[5] = 5  # program count
    f[6] = p.STATUS_DISCHARGING  # status
    struct.pack_into(">H", f, 7, 3600)  # elapsed s
    struct.pack_into(">H", f, 9, 3700)  # 3.700 V
    struct.pack_into(">H", f, 11, 1500)  # 1.500 A
    struct.pack_into(">H", f, 13, 1234)  # 1234 mAh
    f[15] = 27  # temperature
    struct.pack_into(">H", f, 16, 50)  # 50 mOhm
    f[18] = 0
    f[-1] = p.checksum(f[:-1])
    return bytes(f)


def test_decode_progress_offsets():
    r = p.decode_progress(_known_progress_frame())
    assert r.slot == 2
    assert r.battery_type == "LiIon"
    assert r.mode == "Discharge"
    assert r.status == "Discharging"
    assert r.elapsed_s == 3600
    assert r.voltage_mv == 3700
    assert r.current_ma == 1500
    assert r.capacity_mah == 1234
    assert r.temperature == 27
    assert r.resistance_mohm == 50
    assert r.occupied and r.is_discharging


def test_encode_decode_roundtrip():
    original = p.SlotReading(
        slot=1,
        battery_type_code=0,
        mode_code=0,
        program_count=2,
        status_code=p.STATUS_CHARGING,
        elapsed_s=120,
        voltage_mv=4100,
        current_ma=1000,
        capacity_mah=880,
        temperature=30,
        resistance_mohm=42,
        led_bits=0x10,
    )
    frame = p.encode_progress(original)
    assert len(frame) == p.FRAME_LEN_USB
    assert p.valid_checksum(frame)
    decoded = p.decode_progress(frame)
    assert decoded == original


def test_resistance_sentinels_become_none():
    for sentinel in (0, 1, 0xFFFF):
        f = bytearray(_known_progress_frame())
        struct.pack_into(">H", f, 16, sentinel)
        f[-1] = p.checksum(f[:-1])
        assert p.decode_progress(bytes(f)).resistance_mohm is None


def test_bad_checksum_rejected():
    f = bytearray(_known_progress_frame())
    f[-1] ^= 0xFF
    with pytest.raises(p.ProtocolError):
        p.decode_progress(bytes(f))


def test_wrong_opcode_rejected():
    f = bytearray(_known_progress_frame())
    f[1] = p.CMD_SLOT_SETTINGS
    f[-1] = p.checksum(f[:-1])
    with pytest.raises(p.ProtocolError):
        p.decode_progress(bytes(f))


# --- read-only safety ------------------------------------------------------
def test_read_commands_build_ok():
    for cmd in (p.CMD_SLOT_PROGRESS, p.CMD_SLOT_SETTINGS, p.CMD_MACHINE_INFO):
        ble = p.build_ble_request(cmd, 0)
        usb = p.build_usb_request(cmd, 0)
        assert p.valid_checksum(ble)
        assert ble[0] == usb[0] == p.HEADER


@pytest.mark.parametrize("control", [0x05, 0xFE, 0x11])
def test_control_commands_are_refused(control):
    with pytest.raises(p.ReadOnlyViolation):
        p.build_ble_request(control, 0)
    with pytest.raises(p.ReadOnlyViolation):
        p.build_usb_request(control, 0)


def test_usb_request_framing():
    # matches the DataExplorer/jaypikay form: 0f 04 55 00 <slot> <chk> ff ff
    req = p.build_usb_request(p.CMD_SLOT_PROGRESS, 3)
    assert req[:5] == bytes([0x0F, 0x04, 0x55, 0x00, 0x03])
    assert req[5] == (0x55 + 3) & 0xFF
    assert req[6:8] == bytes([0xFF, 0xFF])


def test_machine_info_roundtrip():
    frame = p.encode_machine_info(b"MOCKSERIAL12345".hex())
    info = p.decode_machine_info(frame)
    assert bytes.fromhex(info.serial) == b"MOCKSERIAL12345"[:15]
