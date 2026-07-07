"""Poll one bay of a SkyRC MC3000 charger/analyzer (read-only)."""

from __future__ import annotations

import importlib.util
import logging
from typing import Any, Mapping

from .protocol import SLOT_COUNT, MachineInfo, SlotReading
from .reader import Mc3000Reader
from .transports import Transport, build_transport

__all__ = ["Mc3000Source", "sample_from_reading"]

logger = logging.getLogger(__name__)

_TRANSPORT_KINDS = ("ble", "usb", "mock")
#: transport kind -> (importable module that must be present, pip extra that provides it)
_HW_DEPS = {"ble": ("bleak", "mc3000-ble"), "usb": ("usb", "mc3000-usb")}

_INSTRUMENT_MODEL = "SkyRC MC3000"


def _module_available(name: str) -> bool:
    """True when ``import name`` would succeed. Does not import the module."""
    return importlib.util.find_spec(name) is not None


def sample_from_reading(
    reading: SlotReading, *, time_base: str = "collection"
) -> dict[str, float | int | str]:
    """Map one decoded :class:`SlotReading` (device units) to BDF columns.

    Conversions:

    * ``voltage_volt``: ``voltage_mv`` / 1000.
    * ``current_ampere``: ``current_ma`` / 1000, signed (see below).
    * ``cumulative_capacity_ah``: ``capacity_mah`` / 1000, signed like the
      current (the device resets the counter at the start of each program leg).
    * ``surface_temperature_celsius``: ``temperature`` as-is (the MC3000's
      per-bay battery sensor, whole degrees; the unit follows the machine's
      display setting and is Celsius on the factory default).

    Timebase (``time_base``): with the default ``"collection"`` the sample
    carries **no** ``test_time_second`` and the harvester stamps elapsed
    collection time -- always monotonic, works while the bay idles. With
    ``"device"`` the sample carries the device's own program run timer
    (``elapsed_s``), which aligns rows with the running charge/discharge
    program but is 0 whenever no program runs, resets per program leg, and
    wraps after 65535 s (~18.2 h). Verified on hardware: an idle bay reports
    a constant 0, which is why "device" is not the default.

    Sign rule (BDF convention: positive current charges the cell): the MC3000
    reports current and accumulated capacity as unsigned magnitudes, and the
    same frame's status byte says which way the current flows. Status
    ``Discharging`` makes both negative; every other status (charging, pause,
    standby, completed -- where the magnitude is ~0 anyway) leaves them
    positive.
    """
    sign = -1.0 if reading.is_discharging else 1.0
    sample: dict[str, float | int | str] = {
        "voltage_volt": reading.voltage_mv / 1000.0,
        "current_ampere": sign * reading.current_ma / 1000.0,
        "cumulative_capacity_ah": sign * reading.capacity_mah / 1000.0,
        "surface_temperature_celsius": float(reading.temperature),
    }
    if time_base == "device":
        sample["test_time_second"] = float(reading.elapsed_s)
    return sample


class Mc3000Source:
    """Read one bay of a SkyRC MC3000 charger/analyzer.

    The MC3000 is a four-bay charger, but a BDF file describes a single cell,
    so each ``Mc3000Source`` instance watches exactly **one** bay. ``slot``
    uses the device's internal 0-based numbering: ``slot=0`` is the leftmost
    bay, which the unit's display labels channel 1 (``slot=n`` is channel
    ``n + 1``). To collect the second bay from the left, for example, run
    ``battfeed collect --source mc3000 --opt slot=1 ...`` -- one run per
    occupied bay.

    Field mapping and unit conversions are documented on
    :func:`sample_from_reading`; per the BDF sign convention, positive current
    charges the cell and the charge/discharge direction is derived from the
    slot status reported in the same frame. ``test_time_second`` is the
    device's own elapsed-run-time counter, preferred over the harvester's
    elapsed-collection stamp.

    The connection is lazy: nothing talks to the device until the first
    :meth:`poll` (or :meth:`metadata`), and :meth:`close` disconnects
    (idempotent; a later poll reconnects). A transient device failure makes
    :meth:`poll` raise, which is deliberate -- the battfeed harvester's error
    policy owns retry and backoff.

    Safety: strictly read-only. Only the read opcodes allowlisted in
    :mod:`battfeed.sources.mc3000.protocol` can ever be sent; the frame
    builders refuse control commands (start/stop/write-settings).

    Args:
        slot: Bay to watch, an integer 0-3 (0 = leftmost bay, the device's
            channel 1).
        transport: ``"ble"`` (default; needs ``pip install
            "battfeed[mc3000-ble]"``), ``"usb"`` (needs ``pip install
            "battfeed[mc3000-usb]"``) or ``"mock"`` (a built-in simulated
            charger -- no hardware, no extras).
        address: BLE device address of the charger, e.g.
            ``"AA:BB:CC:DD:EE:FF"``. Required for ``transport="ble"``,
            ignored otherwise.
        time_base: ``"collection"`` (default) leaves ``test_time_second`` to
            the harvester's collection clock; ``"device"`` uses the charger's
            own program run timer instead (see :func:`sample_from_reading`
            for the trade-off).
    """

    def __init__(
        self,
        slot: int = 0,
        transport: str = "ble",
        address: str | None = None,
        time_base: str = "collection",
    ) -> None:
        transport = str(transport).lower()
        if transport not in _TRANSPORT_KINDS:
            raise ValueError(
                f"transport must be one of {list(_TRANSPORT_KINDS)}, got {transport!r}"
            )
        time_base = str(time_base).lower()
        if time_base not in ("collection", "device"):
            raise ValueError(f'time_base must be "collection" or "device", got {time_base!r}')
        if not isinstance(slot, int) or isinstance(slot, bool) or not 0 <= slot < SLOT_COUNT:
            raise ValueError(
                f"slot must be an integer 0-{SLOT_COUNT - 1} (0-based: slot=0 is the "
                f"leftmost bay, the device's channel 1); got {slot!r}"
            )
        if transport == "ble" and not address:
            raise ValueError(
                'transport="ble" needs the charger\'s BLE address, e.g. address="AA:BB:CC:DD:EE:FF"'
            )
        dep = _HW_DEPS.get(transport)
        if dep is not None and not _module_available(dep[0]):
            module, extra = dep
            raise ImportError(
                f"Mc3000Source(transport={transport!r}) needs the optional {module!r} "
                f"package, which is not installed. Install it with: "
                f'pip install "battfeed[{extra}]".'
            )
        self.name = "mc3000"
        self._slot = slot
        self._transport_kind = transport
        self._address = address
        self._time_base = time_base
        self._transport: Transport | None = None
        self._reader: Mc3000Reader | None = None
        self._machine_info: MachineInfo | None = None
        self._machine_info_attempted = False

    @classmethod
    def availability(cls) -> str | None:
        """Return None if this source can run here, else a human-readable reason.

        Used by the CLI ``sources`` listing; checks for the optional hardware
        libraries without importing them (and never touches hardware). The
        ``mock`` transport works either way.
        """
        missing = [
            f'{kind.upper()} needs pip install "battfeed[{extra}]"'
            for kind, (module, extra) in _HW_DEPS.items()
            if not _module_available(module)
        ]
        if not missing:
            return None
        return "; ".join(missing) + ' (transport="mock" needs neither)'

    def metadata(self) -> Mapping[str, Any]:
        info = self._read_machine_info()
        return {
            "source": self.name,
            "kind": "skyrc-mc3000",
            "instrument_model": _INSTRUMENT_MODEL,
            "transport": self._transport_kind,
            "address": self._address,
            "slot": self._slot,
            "channel": self._slot + 1,
            "serial": info.serial if info else None,
            "firmware": info.firmware if info else None,
            "time_base": self._time_base,
            "notes": (
                "One bay per source; slot is 0-based (slot 0 = channel 1, the "
                "leftmost bay). Voltage from mV; current and cumulative capacity "
                "from unsigned mA/mAh with the direction taken from the slot "
                "status -- positive current = charging (BDF convention). "
                'test_time_second is the harvester collection clock (time_base="collection") '
                'or the device\'s own program run timer (time_base="device").'
            ),
        }

    def poll(self) -> list[dict[str, float | int | str]]:
        """Return one sample for this instance's bay, or ``[]``.

        ``[]`` means the bay has no cell inserted (the device reports a
        0 mV terminal voltage) or a single frame failed to decode. A
        :class:`~battfeed.sources.mc3000.transports.base.TransportError` is
        raised when the device itself is unreachable, so the harvester's
        error policy can retry with backoff -- no retry loop lives here.
        """
        reading = self._ensure_reader().read_slot(self._slot)
        if reading is None or not reading.occupied:
            return []
        return [sample_from_reading(reading, time_base=self._time_base)]

    def close(self) -> None:
        """Disconnect from the device. Idempotent; a later poll() reconnects."""
        transport, self._transport, self._reader = self._transport, None, None
        if transport is not None:
            transport.close()

    # -- internals -----------------------------------------------------------
    def _ensure_reader(self) -> Mc3000Reader:
        """Connect the transport on first use (lazy) and cache the reader."""
        if self._reader is None:
            transport = build_transport(self._transport_kind, self._address)
            try:
                transport.open()
            except Exception:
                try:  # best-effort cleanup: the BLE open spawns a thread
                    transport.close()
                except Exception:  # pragma: no cover - cleanup only
                    logger.debug("transport cleanup close failed", exc_info=True)
                raise
            self._transport = transport
            self._reader = Mc3000Reader(transport)
        return self._reader

    def _read_machine_info(self) -> MachineInfo | None:
        """Fetch and cache the instrument identity; ``None`` when unavailable.

        Attempted exactly once per source instance and tolerant of any
        failure: metadata() must keep working with the identity fields set to
        ``None``. Verified on hardware: some MC3000 firmware never answers
        the machine-info opcode at all (zero reply bytes), so retrying would
        only add a poll-timeout of dead air to the start of every run.
        """
        if not self._machine_info_attempted:
            self._machine_info_attempted = True
            try:
                self._machine_info = self._ensure_reader().read_machine_info()
            except Exception as exc:
                logger.info("MC3000 machine info unavailable: %s", exc)
        return self._machine_info
