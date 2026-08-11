# Tail a live instrument log

Plenty of lab instruments cannot be polled, but happily append rows to a CSV log while they run. In this tutorial you will turn such a growing file into a conforming BDF feed with the `csvtail` source — using a small script as a stand-in instrument, so you can run everything before pointing it at real equipment.

This continues from [Your first feed](first-feed.md); you should know what a collect run and its output files look like.

## 1. Stand up a fake instrument

Save as `feeder.py`. It appends one reading per second to `instr.log`, the way a logging DMM or cycler export might:

```python
"""Pretend to be an instrument: append one reading per second to instr.log."""
import time

with open("instr.log", "w", buffering=1) as f:
    f.write("timestamp,V,I,T\n")
    for step in range(45):
        f.write(f"{step},{4.05 - 0.002*step:.4f},{-1.2:.3f},{24.5 + 0.01*step:.2f}\n")
        time.sleep(1)
```

Start it and leave it running:

```bash
python feeder.py &
```

Note what the "instrument" writes: its own column names (`V`, `I`, `T`), no BDF anywhere.

## 2. Tail it into a BDF feed

`csvtail` needs to be told, explicitly, how the log's columns map to canonical BDF names — it will never guess (vendor-format guessing is [batterydf](https://github.com/battery-data-alliance)'s job, and the [non-goals](../explanation/design.md) explain why battfeed refuses it):

```console
$ battfeed collect --source csvtail \
    --opt path=instr.log \
    --opt 'column_map={"V":"voltage_volt","I":"current_ampere","T":"surface_temperature_celsius"}' \
    --duration 15 --interval 2 --institution LOCAL --cell InstrCell
Collected 17 sample(s) from 'csvtail' in 15.0 s -> LOCAL__InstrCell__20260811_001.bdf.csv
```

Seventeen samples in fifteen seconds at a two-second poll: each poll picked up every row that had appeared since the last one, including the backlog present when collection started. Nothing is skipped and nothing is read twice.

## 3. Inspect the result

```console
$ head -5 LOCAL__InstrCell__20260811_001.bdf.csv
test_time_second,voltage_volt,current_ampere,surface_temperature_celsius
0.015000000013969839,4.05,-1.2,24.5
0.015000000013969839,4.048,-1.2,24.51
0.015000000013969839,4.046,-1.2,24.52
2.031000000075437,4.044,-1.2,24.53
```

Two things to notice. The instrument's columns arrived renamed to canonical BDF, in the required order. And the first three rows share one `test_time_second`: the instrument's own `timestamp` column was *not* mapped, so the harvester stamped each sample with elapsed collection time — rows that arrived in the same poll batch share a stamp. If your log carries a usable time column, map it to `test_time_second` and the source's own timeline is used instead; if its units aren't seconds, `--opt 'unit_scale={"test_time_second": 0.001}'` rescales (milliseconds, in that example).

## 4. Where you are now

Any instrument that can append to a file is now a BDF feed. For real deployments the pieces you will add are a [config file](../howto/config-files.md) instead of that long command line, and a [service wrapper](../howto/run-unattended.md) so collection survives reboots. If your instrument *pushes* readings over BLE or a socket instead of appending to a file, that is what [Write your own source](custom-source.md) and its streaming section are for.
