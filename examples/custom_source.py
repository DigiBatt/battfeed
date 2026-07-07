"""A minimal third-party data source implementing the battfeed DataSource protocol.

Note that RampSource imports nothing from battfeed: the protocol is structural
(typing.Protocol), so `name`, `metadata()` and `poll()` are all it takes. A real
collector would talk to hardware inside poll(); this one just ramps a voltage.

To make a packaged source discoverable by `battfeed sources` / `create_source`,
declare an entry point in your own pyproject.toml:

    [project.entry-points."battfeed.sources"]
    ramp = "my_pkg.sources:RampSource"
"""

from battfeed import BdfCsvSink, Harvester


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


if __name__ == "__main__":
    harvester = Harvester()
    harvester.register(RampSource())
    sink = BdfCsvSink("LOCAL__RampCell__20260707_001.bdf.csv")
    stats = harvester.collect("ramp", duration_s=5, interval_s=0.5, sink=sink)
    sink.close()
    print(f"Wrote {stats.samples} samples; columns: {stats.columns}")
