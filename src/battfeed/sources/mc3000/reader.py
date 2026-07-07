"""High-level reader -- turns a transport into decoded :class:`SlotReading` objects.

This is the read-only harvesting core: it polls a slot's real-time measurement
frame and decodes it. A *decode* failure for a single slot is swallowed
(returned as ``None``) so one malformed frame never stops a run; a *transport*
failure (device unreachable, timeout) propagates, because retry/backoff is the
caller's job -- in battfeed that is the harvester's ``ErrorPolicy``.
"""

from __future__ import annotations

import logging

from .protocol import (
    CMD_MACHINE_INFO,
    CMD_SLOT_PROGRESS,
    SLOT_COUNT,
    MachineInfo,
    ProtocolError,
    SlotReading,
    decode_machine_info,
    decode_progress,
)
from .transports.base import Transport, TransportError

__all__ = ["Mc3000Reader"]

logger = logging.getLogger(__name__)


class Mc3000Reader:
    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def read_slot(self, slot: int) -> SlotReading | None:
        """Poll and decode one slot; ``None`` on a decode failure.

        :class:`~battfeed.sources.mc3000.transports.base.TransportError` is NOT
        caught here: a dead link must surface to the caller's error policy
        instead of masquerading as an empty reading.
        """
        frame = self.transport.poll(CMD_SLOT_PROGRESS, slot)
        try:
            return decode_progress(frame)
        except ProtocolError as e:
            logger.warning("slot %d: decode error: %s", slot, e)
            return None

    def read_all(self) -> list[SlotReading]:
        """Poll all four slots. Empty slots are included (occupied=False).

        Undecodable frames are skipped; transport errors propagate.
        """
        out: list[SlotReading] = []
        for slot in range(SLOT_COUNT):
            reading = self.read_slot(slot)
            if reading is not None:
                out.append(reading)
        return out

    def read_machine_info(self) -> MachineInfo | None:
        """Best-effort instrument identity; ``None`` if unavailable."""
        try:
            frame = self.transport.poll(CMD_MACHINE_INFO, 0)
            return decode_machine_info(frame)
        except (TransportError, ProtocolError) as e:
            logger.info("machine info unavailable: %s", e)
            return None
        except Exception as e:  # pragma: no cover - defensive
            logger.info("machine info unavailable (%s)", e)
            return None
