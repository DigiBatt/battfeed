# Write your own source

battfeed's reason for existing is the sources *you* write: the cycler in your lab, the BMS on your bench. In this tutorial you will implement a source from scratch, prove it correct with the shipped contract test, run it, and see what it takes to publish it as a plugin. The example is deliberately trivial — a voltage ramp — so every line is about the contract, not the device.

## 1. The whole contract is three members

`battfeed.DataSource` is a `typing.Protocol` — the seam is structural, so your class imports **nothing** from battfeed. Save as `custom_source.py`:

```python
class RampSource:
    name = "ramp"

    def __init__(self) -> None:
        self._step = 0

    def metadata(self):
        return {"source": self.name, "vendor": "example", "kind": "synthetic-ramp"}

    def poll(self):
        self._step += 1
        return [
            {
                "voltage_volt": 3.0 + 0.01 * self._step,  # charging: voltage rising
                "current_ampere": 0.05,  # positive = charging (BDF sign convention)
            }
        ]

    def close(self):  # optional hook; battfeed calls it when present
        print("RampSource closed")
```

`name` identifies the source, `metadata()` describes it (this lands in every sidecar), and `poll()` returns zero or more *new* samples since the last call, keyed by canonical BDF column names. A real source would talk to hardware inside `poll()`.

The one rule that matters most: **when the device is unreachable, raise.** Do not write a retry loop — the `Harvester` owns retry, with exponential backoff under a configurable `ErrorPolicy`, and only abandons the run after too many *consecutive* failures. This division of labour is what makes multi-day collection survive flaky Bluetooth, and it only works if sources stay honest about failure. The reasoning gets a full page: [how battfeed survives flaky hardware](../explanation/reliability.md).

## 2. Prove it correct with one line

`battfeed.testing.check_source` asserts everything a pipeline relies on — the protocol surface, strict-JSON metadata and samples, sample shape, routing-key discipline. Save as `test_ramp.py`:

```python
from battfeed.testing import check_source
from custom_source import RampSource

def test_ramp_contract():
    check_source(RampSource())  # drive it with a mock or replay tape, never live hardware
```

```console
$ python -m pytest test_ramp.py -q
.                                                                        [100%]
1 passed in 0.11s
```

That one line is a complete contract test; put it in your own package's suite and battfeed's expectations are enforced on every commit you make.

## 3. Run it

```python
from battfeed import BdfCsvSink, Harvester

harvester = Harvester()
harvester.register(RampSource())
sink = BdfCsvSink("LOCAL__RampCell__20260811_001.bdf.csv")
stats = harvester.collect("ramp", duration_s=5, interval_s=0.5, sink=sink)
sink.close()
print(f"Wrote {stats.samples} samples; columns: {stats.columns}")
```

```console
$ python custom_source.py
Wrote 10 samples; columns: ['current_ampere', 'test_time_second', 'voltage_volt']
```

Ten polls in five seconds, each returning one sample; the harvester stamped `test_time_second` because the source did not supply it.

## 4. Publish it as a plugin

To make your source appear in `battfeed sources` and work with `battfeed collect --source ramp`, declare an entry point in your own package's `pyproject.toml` — battfeed's own built-ins register through this exact mechanism, so it is exercised constantly:

```toml
[project.entry-points."battfeed.sources"]
ramp = "my_pkg.sources:RampSource"
```

Two optional hooks make a source a better citizen: an `availability()` classmethod that explains why the source cannot run *here* (missing extra, wrong platform — this is what produces the `[unavailable: ...]` annotations in `battfeed sources`), and a `discover()` classmethod that lets `battfeed discover` find your devices and print ready-to-paste collect commands.

## 5. Push-style hardware

Devices that *push* readings (BLE notifications, CAN frames, MQTT) don't fit a synchronous `poll()`. Subclass `battfeed.StreamingSource` instead: implement a blocking `run_reader(emit, should_stop)` loop calling `emit(sample)` per reading, and the base class runs it in a background thread and buffers samples for `poll()` to drain — bounded, with overflow counted rather than silently dropped, and a dead reader re-raises at the next `poll()` so the harvester's `ErrorPolicy` still owns retry.

Because BLE/CAN hardware is not on every desk, the supported workflow is **replay-first**: record raw frames from one live session into a JSONL *tape* (`battfeed.testing.TapeRecorder`), commit the tape, and drive every test from it with `ReplayReader` — which compresses time, so an hour-long session replays in milliseconds. See [`examples/streaming_source.py`](https://github.com/DigiBatt/battfeed/blob/main/examples/streaming_source.py) for a complete runnable version.

## 6. Where you are now

You have implemented, verified, run, and (on paper) published a battfeed source. The [API reference](../reference/api.md) documents every object you touched; [design principles](../explanation/design.md) explains why the seam is structural and the core dependency-free.
