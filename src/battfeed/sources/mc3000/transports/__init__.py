"""Transport factory for the MC3000 source."""

from __future__ import annotations

from .base import Transport, TransportError

__all__ = ["Transport", "TransportError", "build_transport"]


def build_transport(kind: str, address: str | None = None) -> Transport:
    """Construct a transport by name: ``"mock"`` | ``"ble"`` | ``"usb"``.

    Hardware transports are imported lazily so ``mock`` never requires
    bleak/pyusb. ``address`` is the BLE device address; the other transports
    ignore it.
    """
    kind = kind.lower()
    if kind == "mock":
        from .mock import MockTransport

        return MockTransport()
    if kind == "ble":
        from .ble import BleTransport

        if not address:
            raise TransportError("the BLE transport needs a device address")
        return BleTransport(address=address)
    if kind == "usb":
        from .usb import UsbTransport

        return UsbTransport()
    raise TransportError(f"unknown transport {kind!r} (expected 'mock', 'ble' or 'usb')")
