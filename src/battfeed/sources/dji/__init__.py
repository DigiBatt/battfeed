"""DJI flight-log import source.

Ported from a proprietary implementation (with the maintainer's authorization)
into Apache-2.0 battfeed. The parser wraps the external ``dji-log`` CLI and the
plausibility gate is kept intact; only the output contract is adapted to BDF.
Importing this package is cheap -- the ``dji-log`` binary is only invoked when
the source polls.
"""

from .source import DjiFlightLogSource

__all__ = ["DjiFlightLogSource"]
