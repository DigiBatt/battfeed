"""Transport abstraction -- bytes in, bytes out.

A transport knows how to (a) build a request frame in its own framing (BLE vs
USB) and (b) exchange it with the device, returning the raw response frame. All
protocol *meaning* lives in :mod:`battfeed.sources.mc3000.protocol`; a transport
only moves bytes. Because request frames are built exclusively through the
read-only builders in ``protocol``, a transport physically cannot emit a
control command.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

__all__ = ["Transport", "TransportError"]


class Transport(ABC):
    #: "ble" or "usb" -- selects the request framing and expected frame length.
    frame_kind: str = "usb"

    #: Whether the machine-info opcode (0x5a) can produce a decodable reply on
    #: this transport. The reader skips the request entirely when False, so a
    #: transport that cannot answer does not cost a poll timeout per run.
    supports_machine_info: bool = True

    @abstractmethod
    def open(self) -> None:
        """Acquire the device. Idempotent."""

    @abstractmethod
    def close(self) -> None:
        """Release the device. Idempotent."""

    @abstractmethod
    def poll(self, cmd: int, slot: int) -> bytes:
        """Send a read request for ``(cmd, slot)`` and return the raw response.

        Implementations MUST build the request via ``protocol.build_ble_request``
        / ``protocol.build_usb_request`` (which enforce the read-only allowlist).
        """

    # convenience: usable as a context manager
    def __enter__(self) -> Transport:
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class TransportError(Exception):
    """A recoverable transport-layer failure (timeout, disconnect, no device)."""
