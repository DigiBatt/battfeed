"""battfeed turns live battery data sources into BDF (Battery Data Format) feeds.

Acquisition layer of the open battery-data stack: implement a
:class:`DataSource`, point a :class:`Harvester` at it, and get conforming
``.bdf.csv`` files out. Normalisation of exported vendor files is the job
of the ``batterydf`` package (Battery Data Alliance), not of battfeed.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("battfeed")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.4.0"

from .harvester import CollectStats, ErrorPolicy, Harvester, SourceFailure
from .importer import ImportStats, run_import
from .ingest_state import ImportLedger
from .protocols import RESERVED_KEYS, DataSource, Sample, SampleValue, Sink
from .registry import available_sources, create_source
from .sinks.bdf_csv import BdfCsvSink
from .sinks.routing import RoutingSink
from .sources.streaming import DeadReaderError, StreamingSource

__all__ = [
    "DataSource",
    "Sink",
    "Sample",
    "SampleValue",
    "RESERVED_KEYS",
    "Harvester",
    "CollectStats",
    "ErrorPolicy",
    "SourceFailure",
    "ImportLedger",
    "ImportStats",
    "run_import",
    "StreamingSource",
    "DeadReaderError",
    "BdfCsvSink",
    "RoutingSink",
    "available_sources",
    "create_source",
    "__version__",
]
