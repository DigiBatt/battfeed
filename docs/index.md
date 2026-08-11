# battfeed

**battfeed turns live battery data sources into BDF (Battery Data Format) feeds.**

It is the acquisition layer of an open battery-data stack: point it at something that produces battery readings — a simulator, a growing instrument log, the battery in your Windows laptop, or your own cycler driver — and it polls that source on a schedule and writes conforming `.bdf.csv` files (plus a `.meta.json` sidecar) that the rest of the stack understands.

```
  cyclers · instruments · OS batteries · live logs · simulators
                        │
                        │  poll()                (you implement DataSource)
                        ▼
                     battfeed                     (this package: Harvester + sinks)
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

The core has **zero runtime dependencies** — everything is Python standard library. Requires Python 3.10 or newer.

## Installation

```bash
pip install battfeed
```

Optional capability lives behind extras; the core never grows a dependency:

| Extra | Installs | Enables |
|---|---|---|
| `battfeed[wmi]` | wmi | `wmi` source: the local Windows laptop/tablet battery |
| `battfeed[mc3000-ble]` | bleak | SkyRC MC3000 charger over Bluetooth LE (the `mock` transport needs no extra) |
| `battfeed[mc3000-usb]` | pyusb | SkyRC MC3000 over USB |
| `battfeed[parquet]` | pyarrow | `ParquetSink`: write captures as Parquet for analysis |
| `battfeed[bdf]` | batterydf | `validate_file()`: check output against the BDF reference implementation |
| `battfeed[dev]` | pytest, ruff, mypy | the development toolchain |

The `android` source needs only the `adb` executable on your PATH; the `dji` import source needs the external `dji-log` binary. Neither needs a Python extra.

## Documentation

The documentation follows the [Diátaxis](https://diataxis.fr/) model: pick the section that matches what you need right now.

::::{grid} 1 1 2 2
:gutter: 3

:::{grid-item-card} 🎓 Tutorials
:link: tutorials/index
:link-type: doc

Learning-oriented lessons with real command output: collect your first feed, tail a live instrument log, write your own source.
:::

:::{grid-item-card} 🛠 How-to guides
:link: howto/index
:link-type: doc

Task-oriented recipes: config files and secrets, unattended operation, importing logged files, validating output, choosing a sink.
:::

:::{grid-item-card} 📖 Reference
:link: reference/index
:link-type: doc

Information-oriented: every CLI command, every built-in source, the output file contract, the full Python API, and the changelog.
:::

:::{grid-item-card} 💡 Explanation
:link: explanation/index
:link-type: doc

Understanding-oriented: the design rules that hold battfeed together, and how it survives flaky hardware without losing data.
:::

::::

## What battfeed is not

Keeping battfeed small is the point. It deliberately does **not** do vendor-file normalization (parsing exported Neware / BioLogic / Digatron files is [batterydf](https://github.com/battery-data-alliance)'s job), upload or fleet-management services (battfeed runs no server; `HttpPushSink` is a one-way POST to an endpoint you choose), or digital-twin and model logic (that is [battwin](https://github.com/DigiBatt/battwin), which consumes battfeed's files). The [design principles](explanation/design.md) page explains where the lines are drawn and why.

## Related projects

| Project | Role relative to battfeed |
|---|---|
| [BDF / batterydf](https://github.com/battery-data-alliance) | the format battfeed emits, and the toolchain for files at rest |
| [battwin](https://github.com/DigiBatt/battwin) | battery digital twins that link the datasets battfeed collects |
| [BattINFO](https://github.com/BIG-MAP/BattINFO) | semantic records and ontology the wider stack grounds in |

## Acknowledgements

This project has received support from European Union research and innovation programs under grant agreement [101103997 – DigiBatt](https://digibattproject.eu/).

battfeed is Apache-2.0 licensed. See [LICENSE](https://github.com/DigiBatt/battfeed/blob/main/LICENSE) and [NOTICE](https://github.com/DigiBatt/battfeed/blob/main/NOTICE).

```{toctree}
:hidden:

tutorials/index
howto/index
reference/index
explanation/index
project/index
```
