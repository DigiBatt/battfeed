"""Discovery of installed data sources.

Sources are found in two places:

1. The built-in sources shipped with battfeed (simulator, csvtail, wmi).
2. The ``"battfeed.sources"`` entry-point group, which any installed package
   can contribute to::

       [project.entry-points."battfeed.sources"]
       my-cycler = "my_pkg.sources:MyCyclerSource"

battfeed registers its own built-ins through the same entry-point group (see
``pyproject.toml``), so the plugin mechanism is exercised on every install;
the built-in table below only guarantees discovery when battfeed is imported
from a source tree without being installed.
"""

from __future__ import annotations

import logging
from importlib import import_module
from importlib.metadata import entry_points

__all__ = ["available_sources", "create_source"]

logger = logging.getLogger(__name__)

#: Built-in sources, as "module:ClassName" targets (same format as entry points).
_BUILTINS: dict[str, str] = {
    "simulator": "battfeed.sources.simulator:SimulatedCellSource",
    "csvtail": "battfeed.sources.csvtail:CsvTailSource",
    "wmi": "battfeed.sources.wmi_battery:WmiBatterySource",
    "mc3000": "battfeed.sources.mc3000:Mc3000Source",
    "android": "battfeed.sources.android:AndroidBatterySource",
    "dji": "battfeed.sources.dji:DjiFlightLogSource",
}


def _load(target: str) -> type:
    module_name, _, attr = target.partition(":")
    return getattr(import_module(module_name), attr)


def available_sources() -> dict[str, type]:
    """Return every discoverable source class, keyed by source name.

    Built-ins are always present; entry points contribute additional names.
    A broken source is skipped with a warning rather than breaking
    discovery for everyone else.
    """
    found: dict[str, type] = {}
    for name, target in _BUILTINS.items():
        try:
            found[name] = _load(target)
        except Exception:  # pragma: no cover - only hit with a broken install
            logger.warning("Skipping broken built-in source %r (%s)", name, target)
    for ep in entry_points(group="battfeed.sources"):
        if ep.name in found:
            continue  # built-ins win; also dedupes battfeed's own entry points
        try:
            found[ep.name] = ep.load()
        except Exception:  # pragma: no cover - depends on installed plugins
            logger.warning("Skipping broken 'battfeed.sources' entry point %r", ep.name)
    return found


def create_source(name: str, **kwargs):
    """Instantiate the source registered under ``name``.

    Keyword arguments are passed through to the source constructor, e.g.
    ``create_source("csvtail", path="log.csv", column_map={...})``.

    Raises:
        KeyError: if no source is registered under ``name``.
    """
    sources = available_sources()
    try:
        cls = sources[name]
    except KeyError:
        raise KeyError(f"Unknown source {name!r}. Available sources: {sorted(sources)}") from None
    return cls(**kwargs)
