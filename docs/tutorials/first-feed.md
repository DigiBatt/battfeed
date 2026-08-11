# Your first feed

In this tutorial you will collect battery data with battfeed, look at exactly what lands on disk, and validate it against the BDF reference implementation. No hardware needed: the built-in simulator behaves like a small coin cell under constant-current discharge.

You need Python 3.10 or newer and about ten minutes.

## 1. Install battfeed

```bash
pip install battfeed
```

```console
$ battfeed --version
battfeed 0.5.0
```

## 2. See what you can collect from

```console
$ battfeed sources
android    Read an Android device battery through ``adb shell`` (dumpsys + sysfs).  [unavailable: requires the Android platform-tools "adb" executable on PATH]
             options: serial=None, adb_path='adb', use_sysfs=True, backend=None
csvtail    Follow a CSV file that another process is appending to.
             options: path, column_map, unit_scale=None, name='csvtail', encoding='utf-8'
dji        Import DJI Fly app flight records as per-(pack, flight) BDF feeds.
             options: path, ledger_path=None, dji_log_bin=None, api_key=None, include_gps=False
mc3000     Read one bay of a SkyRC MC3000 charger/analyzer.  [unavailable: BLE needs pip install "battfeed[mc3000-ble]"; USB needs pip install "battfeed[mc3000-usb]" (transport="mock" needs neither)]
             options: slot=0, transport='ble', address=None, time_base='collection'
simulator  Simulate a CR2032-ish coin cell under constant-current discharge.
             options: name='simulator', steps_to_empty=3600, discharge_current_a=0.002, full_voltage_v=3.0, empty_voltage_v=2.0, ambient_c=25.0
wmi        Read the laptop/tablet battery through the Windows ``root\wmi`` classes.
             options: name='wmi'
```

Read this listing carefully, because it teaches battfeed's manners: sources that cannot run *here* say so and say why (`adb` not on PATH; a Bluetooth extra not installed) instead of failing later, and every source advertises its constructor options right where you need them.

## 3. Collect ten seconds of data

```console
$ battfeed collect --source simulator --duration 10 --interval 1 --institution LOCAL --cell DemoCell
Collected 10 sample(s) from 'simulator' in 10.0 s -> LOCAL__DemoCell__20260811_001.bdf.csv
```

Omit `--duration` and collection runs until Ctrl-C, which stops gracefully and finalises the files — that is the mode real collectors run in.

## 4. Look at what landed on disk

Two files appeared, and their names are part of the contract (`Institution__Cell__YYYYMMDD_NNN`):

```console
$ ls
LOCAL__DemoCell__20260811_001.bdf.csv
LOCAL__DemoCell__20260811_001.meta.json
```

The data file is a conforming BDF CSV. The required trio `test_time_second, voltage_volt, current_ampere` leads the header, extra columns follow alphabetically, and the current is negative because the cell is discharging (BDF sign convention: positive current charges):

```console
$ head -4 LOCAL__DemoCell__20260811_001.bdf.csv
test_time_second,voltage_volt,current_ampere,surface_temperature_celsius
0.0,3.0,-0.002,25.0
1.0,3.0,-0.002,25.016664
2.0,3.0,-0.002,25.033309
```

The sidecar records everything about *how* the data was collected — the source and its self-description, your run parameters, timestamps, the columns, the row count:

```json
{
  "file": "LOCAL__DemoCell__20260811_001.bdf.csv",
  "metadata": {
    "institution": "LOCAL",
    "cell_name": "DemoCell",
    "source": {
      "source": "simulator",
      "kind": "simulated",
      "cell": "CR2032-like coin cell (synthetic)",
      "chemistry": "Li-MnO2 (synthetic)",
      "sign_convention": "positive current charges the cell (BDF)"
    },
    "requested_duration_second": 10.0,
    "requested_interval_second": 1.0
  },
  "started_at": "2026-08-11T13:08:50+00:00",
  "finished_at": "2026-08-11T13:09:00+00:00",
  "battfeed_version": "0.5.0",
  "columns": ["test_time_second", "voltage_volt", "current_ampere", "surface_temperature_celsius"],
  "rows": 10,
  "finalized": true
}
```

That last field matters operationally: the sidecar is written *early* with `"finalized": false` and flipped to `true` only on a clean exit, so downstream tooling can tell a finished capture from a crashed one just by reading it. The [unattended-operation guide](../howto/run-unattended.md) builds on exactly this.

## 5. The same thing from Python

The CLI is a thin wrapper over three objects you can use directly:

```python
from battfeed import BdfCsvSink, Harvester, create_source

harvester = Harvester()
harvester.register(create_source("simulator"))
sink = BdfCsvSink("LOCAL__DemoCell__20260811_002.bdf.csv", metadata={"operator": "me"})
harvester.collect("simulator", duration_s=10, interval_s=1.0, sink=sink)
sink.close()  # finalises the CSV and writes the .meta.json sidecar
```

## 6. Validate the output

Don't take battfeed's word for it that the file conforms — check it against the BDF reference implementation:

```bash
pip install "battfeed[bdf]"
```

```python
>>> from battfeed.sinks.bdf_csv import validate_file
>>> validate_file("LOCAL__DemoCell__20260811_001.bdf.csv")
{'ok': True, 'missing': [], 'extras': ['surface_temperature_celsius'], ...}
```

`ok: True`, nothing missing; the temperature column is a legitimate extra beyond the required trio.

## 7. Where you are now

You have produced, understood, and independently validated a BDF capture. From here:

- [Tail a live instrument log](tail-a-log.md) replaces the simulator with a real, growing file.
- [Write your own source](custom-source.md) is the tutorial for connecting your own hardware.
- If your machine is a Windows laptop, [collect its own battery](../howto/laptop-battery.md) right now — it is one command.
