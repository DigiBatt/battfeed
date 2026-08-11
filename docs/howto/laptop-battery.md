# Collect your laptop's own battery (Windows)

The `wmi` source reads the local Windows laptop or tablet battery through the `root\wmi` classes — the quickest way to collect *real* hardware data with no equipment at all.

## Install and run

```bash
pip install "battfeed[wmi]"
```

```console
$ battfeed collect --source wmi --duration 6 --interval 2 --institution SINTEF --cell LaptopMain
Collected 3 sample(s) from 'wmi' in 6.0 s -> SINTEF__LaptopMain__20260811_001.bdf.csv

$ head -4 SINTEF__LaptopMain__20260811_001.bdf.csv
test_time_second,voltage_volt,current_ampere,power_watt
0.09399999992456287,16.59,0.0,0.0
2.1089999999385327,16.59,0.0,0.0
4.155999999959022,16.59,0.0,0.0
```

That is a genuine reading from the machine these docs were built on: a 4-cell pack at 16.59 V, drawing no battery current because the laptop was on mains power. Unplug the charger and `current_ampere` goes negative (discharging, per the BDF sign convention); plug it back in mid-charge and it goes positive.

## Notes

- Windows only, and the machine must actually have a battery — on a desktop the source reports itself unavailable in `battfeed sources` rather than failing mid-run.
- Poll gently: the WMI classes update on the order of seconds, so `--interval 2` or slower is plenty.
- For a long-running record of charge/discharge behaviour (for example, profiling your fleet's laptop packs), combine this with a [config file](config-files.md) and the [unattended-operation recipes](run-unattended.md) — the Windows Task Scheduler recipe there works as-is with `--source wmi`.
- Android phones are the same idea over `adb`: `battfeed collect --source android --interval 5`, needing only platform-tools on PATH. `battfeed discover --source android` lists connected devices.
