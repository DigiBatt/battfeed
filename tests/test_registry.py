from __future__ import annotations

import pytest

from gleaned import available_sources, create_source
from gleaned.sources.simulator import SimulatedCellSource


def test_builtin_sources_are_listed():
    sources = available_sources()
    assert {"simulator", "csvtail", "wmi"} <= set(sources)
    assert all(isinstance(cls, type) for cls in sources.values())


def test_create_source_instantiates_with_kwargs():
    source = create_source("simulator", steps_to_empty=10)
    assert isinstance(source, SimulatedCellSource)
    assert source.name == "simulator"

    with pytest.raises(KeyError, match="Unknown source"):
        create_source("does-not-exist")
