"""BLE discovery of SkyRC MC3000 chargers.

The MC3000 advertises the generic name "Charger" (an HM-10 style serial
bridge), so matching by name is useless; the reliable fingerprint is the
advertised FFE0 serial service. Two physical caveats, both verified on
hardware: a charger that is *connected* to another program (a collector
daemon, the vendor phone app) stops advertising and cannot be discovered
until that connection is closed, and BLE advertising is only visible within
radio range.
"""

from __future__ import annotations

import logging
from typing import Any

from .transports.ble import SERVICE_UUID

__all__ = ["discover_ble"]

logger = logging.getLogger(__name__)


def discover_ble(timeout_s: float = 6.0) -> list[dict[str, Any]]:
    """Scan for advertising MC3000 chargers and return collect-ready candidates.

    Each candidate carries ``option``/``value`` -- the constructor kwarg
    (``address``) and the BLE address to pass to it -- plus descriptive
    fields (``name``, ``rssi_dbm``). Strongest signal first, so the nearest
    charger is the first candidate. Requires the ``battfeed[mc3000-ble]``
    extra; an empty result means no charger was advertising during the scan,
    not that none exists.
    """
    if timeout_s <= 0:
        raise ValueError(f"timeout_s must be positive, got {timeout_s}")
    candidates: list[dict[str, Any]] = [
        {"option": "address", "value": address, "name": name, "rssi_dbm": rssi}
        for address, name, rssi in _ble_scan(timeout_s)
    ]
    candidates.sort(key=lambda c: (c["rssi_dbm"] is None, -(c["rssi_dbm"] or 0)))
    return candidates


def _ble_scan(timeout_s: float) -> list[tuple[str, str | None, int | None]]:
    """One bleak scan, filtered to devices advertising the FFE0 service.

    Returns ``(address, advertised name, RSSI dBm)`` tuples. Isolated so
    tests can replace it with a fake; everything above this call is pure.
    """
    try:
        from bleak import BleakScanner  # lazy, like the BLE transport
    except ImportError as exc:
        raise ImportError(
            'BLE discovery needs the optional "bleak" package; install it '
            'with: pip install "battfeed[mc3000-ble]"'
        ) from exc
    import asyncio

    async def _scan() -> Any:
        return await BleakScanner.discover(timeout=timeout_s, return_adv=True)

    results = asyncio.run(_scan())
    matches: list[tuple[str, str | None, int | None]] = []
    for device, adv in results.values():
        uuids = {str(u).lower() for u in (adv.service_uuids or ())}
        if SERVICE_UUID in uuids:
            matches.append((device.address, device.name, adv.rssi))
            logger.info("MC3000 candidate %s (%s, RSSI %s)", device.address, device.name, adv.rssi)
    return matches
