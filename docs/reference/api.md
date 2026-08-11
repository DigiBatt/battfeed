# API reference

The public API is importable from the top-level `battfeed` package; the modules below are where the objects live. Anything not documented here (names starting with `_`) is internal.

## `battfeed.protocols`: the contracts

```{eval-rst}
.. automodule:: battfeed.protocols
   :members:
```

## `battfeed.harvester`: the collection loop

```{eval-rst}
.. automodule:: battfeed.harvester
   :members:
```

## `battfeed.registry`: source discovery

```{eval-rst}
.. automodule:: battfeed.registry
   :members:
```

## `battfeed.config`: config files and secrets

```{eval-rst}
.. automodule:: battfeed.config
   :members:
```

## `battfeed.importer` and `battfeed.ingest_state`: file import

```{eval-rst}
.. automodule:: battfeed.importer
   :members:

.. automodule:: battfeed.ingest_state
   :members:
```

## Sinks

```{eval-rst}
.. automodule:: battfeed.sinks.bdf_csv
   :members:

.. automodule:: battfeed.sinks.routing
   :members:

.. automodule:: battfeed.sinks.http_push
   :members:

.. automodule:: battfeed.sinks.parquet
   :members:
```

## `battfeed.sources.streaming`: push-style hardware base

```{eval-rst}
.. automodule:: battfeed.sources.streaming
   :members:
```

## `battfeed.testing`: the source author's toolkit

```{eval-rst}
.. automodule:: battfeed.testing.contract
   :members:

.. automodule:: battfeed.testing.replay
   :members:
```
