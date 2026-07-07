"""SkyRC MC3000 charger/analyzer as a gleaned data source (read-only).

Importing this package is cheap: the hardware libraries (``bleak`` for BLE,
``pyusb`` for USB) are only imported when a hardware transport actually
connects, so the built-in ``mock`` transport and source discovery work on a
bare install.
"""

from __future__ import annotations

from .source import Mc3000Source

__all__ = ["Mc3000Source"]
