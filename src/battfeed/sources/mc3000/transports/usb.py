"""USB HID transport (fallback) -- SkyRC MC3000 over USB.

Uses ``pyusb`` (libusb) to match the community references. The MC3000 enumerates
with the unusual id ``VID=0x0000 PID=0x0001`` and exchanges 64-byte reports on
bulk endpoints ``OUT=0x01`` / ``IN=0x81``. Request framing is the ``0f 04 <cmd>``
form; the response payload is decoded with the same layout as BLE.

Windows note: pyusb needs a libusb-compatible driver (e.g. WinUSB via Zadig)
bound to the device, which competes with the HID class driver. If that is
impractical, prefer the BLE transport, or swap this implementation for
``hidapi`` (the device is a HID device). This path is a documented fallback --
verify on hardware.

``pyusb`` is imported lazily. Install with ``pip install "battfeed[mc3000-usb]"``.

Read-only: requests are built exclusively via ``protocol.build_usb_request``.
"""

from __future__ import annotations

import logging
from typing import Any

from ..protocol import FRAME_LEN_USB, build_usb_request
from .base import Transport, TransportError

__all__ = ["UsbTransport"]

logger = logging.getLogger(__name__)

VID = 0x0000
PID = 0x0001
ENDPOINT_OUT = 0x01
ENDPOINT_IN = 0x81


class UsbTransport(Transport):
    frame_kind = "usb"

    def __init__(self, vid: int = VID, pid: int = PID, timeout_ms: int = 2000) -> None:
        self.vid = vid
        self.pid = pid
        self.timeout_ms = timeout_ms
        self._device: Any = None  # usb.core.Device once opened (lazy optional dep)

    def open(self) -> None:
        if self._device is not None:
            return
        try:
            import usb.core  # lazy
            from usb.core import USBError
        except ImportError as e:  # pragma: no cover
            raise TransportError(
                'pyusb not installed -- run pip install "battfeed[mc3000-usb]"'
            ) from e
        device = usb.core.find(idVendor=self.vid, idProduct=self.pid)
        if device is None:
            raise TransportError(
                f"MC3000 not found on USB (VID={self.vid:#06x} PID={self.pid:#06x})"
            )
        try:
            device.get_active_configuration()
        except USBError:
            device.set_configuration()
        self._device = device
        logger.info("USB opened MC3000 (VID=%#06x PID=%#06x)", self.vid, self.pid)

    def poll(self, cmd: int, slot: int) -> bytes:
        if self._device is None:
            self.open()
        request = build_usb_request(cmd, slot)  # already 64 bytes, read-only guarded
        try:
            self._device.write(ENDPOINT_OUT, request, self.timeout_ms)
            data = self._device.read(ENDPOINT_IN, FRAME_LEN_USB, self.timeout_ms)
        except Exception as e:
            raise TransportError(f"USB I/O failed: {e}") from e
        return bytes(data)

    def close(self) -> None:
        if self._device is None:
            return
        try:
            import usb.util

            usb.util.dispose_resources(self._device)
        except Exception:  # pragma: no cover
            pass
        self._device = None
