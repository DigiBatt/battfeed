"""gleaned turns live battery data sources into BDF (Battery Data Format) feeds.

Acquisition layer of the open battery-data stack: implement a
:class:`DataSource`, point a :class:`Harvester` at it, and get conforming
``.bdf.csv`` files out. Normalisation of exported vendor files is the job
of the ``batterydf`` package (Battery Data Alliance), not of gleaned.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("gleaned")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.2.0"

from .harvester import CollectStats, Harvester
from .protocols import DataSource, Sink
from .registry import available_sources, create_source
from .sinks.bdf_csv import BdfCsvSink

__all__ = [
    "DataSource",
    "Sink",
    "Harvester",
    "CollectStats",
    "BdfCsvSink",
    "available_sources",
    "create_source",
    "__version__",
]
