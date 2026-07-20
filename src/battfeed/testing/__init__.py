"""Shipped test kit for source authors: replay tapes and contract checks.

This subpackage ships with battfeed (it is not test-only code) so that
third-party source packages can depend on it from their own test suites:

* :func:`check_source` -- executable essentials of the ``DataSource``
  contract; run it against your source backed by a mock or a tape.
* :class:`ReplayTape` / :class:`ReplayReader` / :class:`TapeRecorder` --
  record raw frames from one live session, replay them forever with no
  hardware and compressed time (see :mod:`battfeed.testing.replay`).
"""

from .contract import check_source
from .replay import ReplayReader, ReplayTape, TapeFrame, TapeRecorder

__all__ = [
    "ReplayReader",
    "ReplayTape",
    "TapeFrame",
    "TapeRecorder",
    "check_source",
]
