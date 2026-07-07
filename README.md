# gleaned

**gleaned turns live battery data sources into BDF (Battery Data Format) feeds.**

It is the acquisition layer of an open battery-data stack: point it at something that
produces battery readings — a simulator, a growing instrument log, the battery in your
Windows laptop, or your own cycler driver — and it polls that source on a schedule and
writes conforming `.bdf.csv` files (plus a `.meta.json` sidecar) that the rest of the
stack understands.

```
  cyclers · instruments · OS batteries · live logs · simulators
                        │
                        │  poll()                (you implement DataSource)
                        ▼
                     gleaned                     (this package: Harvester + sinks)
                        │
                        ▼
        *.bdf.csv  +  *.meta.json                (Battery Data Format files)
                        │
        ┌───────────────┼────────────────────┐
        ▼               ▼                    ▼
    batterydf        BattINFO           BDA registry
  (normalize,      (semantics,           (publish,
   validate,        ontology)             share)
   analyze)
```

The core has **zero runtime dependencies** — everything is Python standard library.
Requires Python >= 3.10.

## Install

```
pip install gleaned
```

| Extra                    | Installs    | Enables                                            |
| ------------------------ | ----------- | -------------------------------------------------- |
| `pip install gleaned[wmi]` | `wmi`       | `WmiBatterySource` (Windows laptop/tablet battery) |
| `pip install gleaned[bdf]` | `batterydf` | `validate_file()` — check emitted files against the BDF reference implementation |
| `pip install gleaned[dev]` | `pytest`, `ruff`, `mypy` | development                          |

## Quickstart

Collect from the built-in simulator into a BDF file:

```python
from gleaned import BdfCsvSink, Harvester, create_source

harvester = Harvester()
harvester.register(create_source("simulator"))
sink = BdfCsvSink("LOCAL__DemoCell__20260707_001.bdf.csv", metadata={"operator": "me"})
harvester.collect("simulator", duration_s=10, interval_s=1.0, sink=sink)
sink.close()  # finalises the CSV and writes the .meta.json sidecar
```

Or from the command line (Ctrl-C stops gracefully and finalises the files):

```
gleaned collect --source simulator --duration 10 --interval 1 --institution LOCAL --cell DemoCell
```

`gleaned sources` lists everything available, including sources contributed by other
installed packages, and marks unavailable ones (e.g. `wmi` off-Windows).

## What comes out

A conforming BDF CSV with snake_case `{quantity}_{unit}` headers. The required trio
`test_time_second, voltage_volt, current_ampere` always leads the header, followed by
any extra columns in alphabetical order. Files are named
`InstitutionCode__CellName__YYYYMMDD_XXX.bdf.csv` (see `gleaned.sinks.bdf_csv.dataset_filename`).

**Sign convention** (per the BDF specification): positive current charges the test
object, negative current discharges it. Power follows the same sign.

If a source does not stamp its own `test_time_second`, the harvester stamps each sample
with the elapsed collection time.

## Writing your own source

`gleaned.DataSource` is a `typing.Protocol` — the stable seam third-party collectors
implement. No imports from gleaned are needed; any object with `name`, `metadata()` and
`poll()` qualifies:

```python
class MyCyclerSource:
    name = "my-cycler"

    def metadata(self):
        return {"source": self.name, "vendor": "ACME", "channel": 3}

    def poll(self):
        # Return zero or more NEW samples since the last call,
        # keyed by canonical BDF column names.
        reading = my_driver.read_channel(3)
        return [{"voltage_volt": reading.volts, "current_ampere": reading.amps}]
```

Register it with a `Harvester` directly, or expose it to the `gleaned` CLI from your
own package via an entry point:

```toml
[project.entry-points."gleaned.sources"]
my-cycler = "my_pkg.sources:MyCyclerSource"
```

Sources may optionally define `close()` to release hardware handles; gleaned calls it
when present. See `examples/custom_source.py` for a runnable version.

## Built-in sources

| Name        | Class                | What it does                                                                 |
| ----------- | -------------------- | ---------------------------------------------------------------------------- |
| `simulator` | `SimulatedCellSource` | Deterministic synthetic CR2032-ish discharge; ideal for demos and tests.    |
| `csvtail`   | `CsvTailSource`       | Tails a growing CSV log; you supply the column map and unit scale factors.  |
| `wmi`       | `WmiBatterySource`    | Polls the local Windows battery via WMI (`gleaned[wmi]`, Windows only).     |

## Non-goals

Keeping gleaned small is the point. It deliberately does **not** do:

- **Vendor-file normalization.** Parsing and harmonising exported Neware / BioLogic /
  Digatron / Basytec / ... files is the job of
  [`batterydf`](https://github.com/battery-data-alliance) (Battery Data Alliance).
  `CsvTailSource` is config-driven on purpose — it will never guess column synonyms.
- **Upload, fleet management, or multi-tenant services.** gleaned writes local files;
  publishing and sharing belong to registry tooling.
- **Digital-twin or model logic.** State estimation and twin orchestration live in
  [`echoed`](https://github.com/DigiBatt/echoed), which consumes gleaned feeds.

## Development

```
pip install -e ".[dev]"
pytest
```

## Acknowledgements

This project has received support from European Union research and innovation
programs under grant agreement 101103997 (DigiBatt).

## License

Apache-2.0
