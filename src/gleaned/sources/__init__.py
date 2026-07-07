"""Built-in data sources.

Importing this package is cheap: the WMI source only imports the optional
``wmi`` dependency when instantiated.
"""

from .csvtail import CsvTailSource
from .simulator import SimulatedCellSource
from .wmi_battery import WmiBatterySource

__all__ = ["CsvTailSource", "SimulatedCellSource", "WmiBatterySource"]
