"""Collect 10 seconds of simulated cell data into a BDF file."""

import datetime

from battfeed import BdfCsvSink, Harvester, create_source
from battfeed.sinks.bdf_csv import dataset_filename

harvester = Harvester()
harvester.register(create_source("simulator"))

out = dataset_filename("LOCAL", "DemoCell", datetime.date.today(), 1)
sink = BdfCsvSink(out, metadata={"operator": "examples/collect_simulated.py"})
stats = harvester.collect("simulator", duration_s=10, interval_s=1.0, sink=sink)
sink.close()

print(f"Wrote {stats.samples} samples to {out}")
