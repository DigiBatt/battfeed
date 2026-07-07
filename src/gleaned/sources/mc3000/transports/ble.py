"""BLE transport (primary) -- SkyRC MC3000 over its HM-10 style serial bridge.

The MC3000 exposes a Nordic/HM-10 UART service: write a request to the FFE1
characteristic and the reply arrives as a notification on the same
characteristic. ``bleak`` is async, so this transport runs a private asyncio
event loop on a background thread and presents a synchronous :meth:`poll` to
the polling loop.

``bleak`` is imported lazily, so the rest of the integration (protocol, mock
transport) has no BLE dependency. Install with
``pip install "gleaned[mc3000-ble]"``.

Read-only: requests are built exclusively via ``protocol.build_ble_request``.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from typing import Any

from ..protocol import (
    FRAME_LEN_BLE,
    HEADER,
    build_ble_request,
    valid_checksum,
)
from .base import Transport, TransportError

__all__ = ["BleTransport"]

logger = logging.getLogger(__name__)

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
CHARACTERISTIC_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"


class BleTransport(Transport):
    frame_kind = "ble"

    def __init__(
        self, address: str, poll_timeout_s: float = 2.0, write_settle_s: float = 0.1
    ) -> None:
        self.address = address
        self.poll_timeout_s = poll_timeout_s
        self.write_settle_s = write_settle_s
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client: Any = None  # bleak.BleakClient once connected (lazy optional dep)
        self._buffer = bytearray()
        self._frames: queue.Queue[bytes] = queue.Queue()
        self._ready = threading.Event()
        self._err: Exception | None = None
        # auto-reconnect state (a bench run can span days; the link must self-heal)
        self._connected = False
        self._reconnecting = False
        self._last_reconnect = 0.0
        self._reconnect_backoff = 3.0  # min seconds between reconnect attempts

    # -- lifecycle -----------------------------------------------------------
    def open(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, name="mc3000-ble", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=25.0):
            raise TransportError("BLE connect timed out")
        if self._err:
            raise TransportError(f"BLE setup failed: {self._err}")
        # Not yet connected is OK: poll() drives reconnect until the device answers.

    def _run_loop(self) -> None:
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
        except Exception as e:  # pragma: no cover - can't even create a loop
            self._err = e
            self._ready.set()
            return
        # Best-effort initial connect. If the device isn't ready yet, don't die --
        # keep the loop alive so poll()'s reconnect can retry (long unattended runs).
        try:
            self._loop.run_until_complete(self._connect())
        except Exception as e:  # pragma: no cover - hardware path
            logger.warning("BLE initial connect failed (%s) -- will keep retrying", e)
            self._connected = False
        self._ready.set()
        self._loop.run_forever()

    async def _connect(self) -> None:
        try:
            from bleak import BleakClient  # lazy
        except ImportError as e:  # pragma: no cover
            raise TransportError(
                'bleak not installed -- run pip install "gleaned[mc3000-ble]"'
            ) from e
        self._client = BleakClient(self.address, disconnected_callback=self._on_disconnect)
        await self._client.connect()
        await self._client.start_notify(CHARACTERISTIC_UUID, self._on_notify)
        self._connected = True
        logger.info("BLE connected to %s", self.address)

    def _on_disconnect(self, _client) -> None:
        # bleak calls this when the peripheral drops. Flag it so the next poll()
        # kicks off a background reconnect instead of failing forever.
        self._connected = False
        logger.warning("BLE link dropped (%s) -- will reconnect", self.address)

    async def _reconnect(self) -> None:
        """Rebuild the client + notify subscription on the loop thread."""
        try:
            from bleak import BleakClient  # lazy

            if self._client is not None:
                try:
                    await self._client.disconnect()
                except Exception:  # best-effort teardown of the dead client
                    pass
            self._buffer.clear()
            self._client = BleakClient(self.address, disconnected_callback=self._on_disconnect)
            await self._client.connect(timeout=15.0)
            await self._client.start_notify(CHARACTERISTIC_UUID, self._on_notify)
            self._connected = True
            logger.info("BLE reconnected to %s", self.address)
        except Exception as e:  # pragma: no cover - hardware path
            logger.warning("BLE reconnect attempt failed: %s", e)
        finally:
            self._reconnecting = False

    def _trigger_reconnect(self) -> None:
        """Schedule one (rate-limited, non-blocking) reconnect on the loop thread."""
        if self._reconnecting or self._loop is None:
            return
        now = time.monotonic()
        if now - self._last_reconnect < self._reconnect_backoff:
            return
        self._last_reconnect = now
        self._reconnecting = True
        asyncio.run_coroutine_threadsafe(self._reconnect(), self._loop)

    def _on_notify(self, _sender, data: bytearray) -> None:
        # Reassemble notification chunks into complete, checksum-valid frames.
        self._buffer.extend(data)
        while True:
            start = self._buffer.find(HEADER)
            if start < 0:
                self._buffer.clear()
                return
            if start > 0:
                del self._buffer[:start]
            if len(self._buffer) < FRAME_LEN_BLE:
                return
            frame = bytes(self._buffer[:FRAME_LEN_BLE])
            if valid_checksum(frame):
                self._frames.put(frame)
                del self._buffer[:FRAME_LEN_BLE]
            else:
                del self._buffer[:1]  # resync past this false start

    # -- I/O -----------------------------------------------------------------
    def poll(self, cmd: int, slot: int) -> bytes:
        if self._loop is None or self._client is None:
            raise TransportError("BLE transport not open")
        if not self._connected:
            self._trigger_reconnect()
            raise TransportError("BLE not connected (reconnecting)")
        request = build_ble_request(cmd, slot)
        # drain any stale frames so we match this request's reply
        with self._frames.mutex:
            self._frames.queue.clear()
        fut = asyncio.run_coroutine_threadsafe(
            self._client.write_gatt_char(CHARACTERISTIC_UUID, request, response=False),
            self._loop,
        )
        try:
            fut.result(timeout=self.poll_timeout_s)
        except Exception as e:
            self._connected = False  # write failed -- treat the link as down
            self._trigger_reconnect()
            raise TransportError(f"BLE write failed: {e}") from e
        return self._await_frame(cmd, slot)

    def _await_frame(self, cmd: int, slot: int) -> bytes:
        end = time.monotonic() + self.poll_timeout_s
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise TransportError(f"BLE read timed out (cmd 0x{cmd:02x} slot {slot})")
            try:
                frame = self._frames.get(timeout=remaining)
            except queue.Empty:
                raise TransportError("BLE read timed out")
            # match on opcode; progress frames also carry the slot at byte 2
            if frame[1] == cmd and (cmd != 0x55 or frame[2] == slot):
                return frame

    def close(self) -> None:
        if self._loop is None:
            return

        async def _disconnect():
            try:
                if self._client:
                    await self._client.stop_notify(CHARACTERISTIC_UUID)
                    await self._client.disconnect()
            except Exception:  # pragma: no cover
                pass

        try:
            fut = asyncio.run_coroutine_threadsafe(_disconnect(), self._loop)
            fut.result(timeout=5.0)
        except Exception:  # pragma: no cover
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5.0)
        self._loop = None
