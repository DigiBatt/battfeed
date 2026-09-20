# Collect your laptop's own battery (Windows)

The `wmi` source reads the local Windows laptop or tablet battery through the `root\wmi` classes — the quickest way to collect *real* hardware data with no equipment at all.

## Install and run

```bash
pip install "battfeed[wmi]"
```

```console
$ battfeed collect --source wmi --duration 6 --interval 2 --institution SINTEF --cell LaptopMain
Collected 3 sample(s) from 'wmi' in 6.0 s -> SINTEF__LaptopMain__20260818_001.bdf.csv

$ head -4 SINTEF__LaptopMain__20260818_001.bdf.csv
test_time_second,voltage_volt,current_ampere,cycle_count,power_watt,state_of_charge_percent
0.1410000000614673,16.899,0.7260192910823126,352,12.269,92.7094097260862
2.297000000020489,16.899,0.7260192910823126,352,12.269,92.7094097260862
4.484000000171363,16.899,0.7260192910823126,352,12.269,92.7094097260862
```

That is a genuine reading from the machine these docs were built on: a 4-cell pack at 16.9 V taking 12.3 W of charge, 93 % full, 352 cycles into its life. Positive current means charging, per the BDF sign convention; unplug the charger and it goes negative.

Current is derived, not measured. The ACPI classes report a charge or discharge *rate* in milliwatt, so `current_ampere` is `power_watt / voltage_volt`.

## What the pack knows about itself

`battfeed discover --source wmi` reads the nameplate without collecting anything:

```console
$ battfeed discover --source wmi
wmi:
  0  (instance_name=ACPI\PNP0C0A\1_0, device_name=Primary, manufacturer=Hewlett-Packard, chemistry=LIon, design_capacity_mwh=94338, full_charged_capacity_mwh=62903, cycle_count=352)
      -> battfeed collect --source wmi --opt instance=0
```

94.3 Wh when it left the factory in 2021, 62.9 Wh today: this pack has lost a third of its capacity. The same fields, plus serial and design voltage, land in the run's `.meta.json` sidecar.

Not every laptop is this forthcoming. `BatteryStaticData` raises a bare "Generic failure" on plenty of firmware, `BatteryCycleCount` often does not exist at all, and the ACPI "unknown" value 0x80000000 can turn up in any rate or capacity field. Each of those is handled by omitting the field, never by reporting a wrong number, so a reticent machine still collects voltage, current, and power.

Machines with two packs get one sample per pack on every poll; `--opt instance=0` (or `--opt instance=1`) pins a single one.

## Notes

- Windows only, and the machine must actually have a battery — on a desktop the source reports itself unavailable in `battfeed sources` rather than failing mid-run.
- Poll gently: the WMI classes update on the order of seconds, so `--interval 2` or slower is plenty.
- For a long-running record of charge/discharge behaviour (for example, profiling your fleet's laptop packs), combine this with a [config file](config-files.md) and the [unattended-operation recipes](run-unattended.md) — the Windows Task Scheduler recipe there works as-is with `--source wmi`.
- Android phones are the same idea over `adb`: `battfeed collect --source android --interval 5`, needing only platform-tools on PATH. `battfeed discover --source android` lists connected devices.
