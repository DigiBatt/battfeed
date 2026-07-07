"""Android ADB battery source.

Importing this package is cheap: everything here is pure standard
library (``subprocess`` around the ``adb`` executable), so there is no
optional dependency to defer.
"""

from .source import AndroidBatterySource

__all__ = ["AndroidBatterySource"]
