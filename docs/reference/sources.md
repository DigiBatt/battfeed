# Built-in sources

Six sources ship with battfeed, registered through the same `battfeed.sources` entry-point group third-party packages use. `battfeed sources` prints this list live, with options and availability for *your* machine:

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

## `simulator`

Deterministic synthetic CR2032-ish discharge; ideal for demos, tests, and service-recipe dry runs. Every option of the synthetic cell (capacity in steps, current, voltage window, ambient) is adjustable, so it can imitate faster or slower cells on demand.

## `csvtail`

Tails a growing CSV log. You supply `column_map` (log column → canonical BDF name) and optional `unit_scale` factors — deliberately config-driven, never guessing column synonyms (vendor-file normalization is batterydf's job). Map a time column to `test_time_second` to use the instrument's own timeline; otherwise the harvester stamps elapsed collection time. Walkthrough: [Tail a live instrument log](../tutorials/tail-a-log.md).

## `wmi` (Windows)

Polls the local laptop/tablet battery via the `root\wmi` classes: voltage, signed current, power. Needs `battfeed[wmi]`, Windows, and a machine that has a battery. Walkthrough: [Collect your laptop's own battery](../howto/laptop-battery.md).

## `mc3000` (SkyRC MC3000 charger/analyzer)

One bay per source instance (`slot=0..3`), over BLE (`battfeed[mc3000-ble]`), USB (`battfeed[mc3000-usb]`), or the built-in `mock` transport that needs no extra and no hardware — a real end-to-end run for demos:

```console
$ battfeed collect --source mc3000 --opt transport=mock --opt slot=1 --duration 5 --interval 1 --institution LOCAL --cell AA-Bay1
Collected 5 sample(s) from 'mc3000' in 5.0 s -> LOCAL__AA-Bay1__20260811_001.bdf.csv

$ head -3 LOCAL__AA-Bay1__20260811_001.bdf.csv
test_time_second,voltage_volt,current_ampere,cumulative_capacity_ah,surface_temperature_celsius
0.0,4.024,-0.5,-0.0,31.0
1.0,4.024,-0.5,-0.0,31.0
```

Supports discovery (`battfeed discover --source mc3000`, BLE scan on the advertised FFE0 service) and `--opt address=auto` single-device auto-selection.

## `android`

Android device battery via `adb shell` (dumpsys + sysfs), pure stdlib — only the platform-tools `adb` executable is needed. `serial=None` re-resolves the device every poll; `serial=auto` pins the single unambiguous device for the run; a fixed serial pins that device. Supports discovery (adb-connected devices plus mDNS wireless-debugging listeners).

## `dji`

Imports DJI Fly flight-log records (`*.txt`/`*.dat`) as one BDF file per (aircraft + battery pack, flight), through `battfeed import` — it is a batch source, not a polled one. Wraps the external `dji-log` binary; quarantines malformed files; v13+ record decryption calls DJI's keychain API. Details: [Import logged files](../howto/import-files.md).

## Third-party sources

Anything installed in the same environment that declares a `battfeed.sources` entry point appears in this list automatically, options and availability included. Writing one is a [tutorial](../tutorials/custom-source.md); the contract is three members and a rule about raising on trouble.
