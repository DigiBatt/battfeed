"""Built-in data sources.

Importing this package is cheap: optional dependencies (``wmi``, ``bleak``,
``pyusb``) are only imported when a source is instantiated, and the ``adb``
executable is only invoked when the Android source polls.
"""

from .android import AndroidBatterySource
from .csvtail import CsvTailSource
from .mc3000 import Mc3000Source
from .simulator import SimulatedCellSource
from .wmi_battery import WmiBatterySource

__all__ = [
    "AndroidBatterySource",
    "CsvTailSource",
    "Mc3000Source",
    "SimulatedCellSource",
    "WmiBatterySource",
]
