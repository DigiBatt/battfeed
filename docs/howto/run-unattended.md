# Running battfeed unattended

`battfeed collect` is a foreground process that ends on Ctrl-C. On a real
collector host ("foundational infrastructure") you want it started at boot,
restarted when it dies, and shut down cleanly so its output files are
finalized. This page gives tested recipes for Linux (systemd) and Windows
(Task Scheduler, NSSM), plus the operational facts you need to reason about
crashes, sleep, and log rotation.

Everything below uses the `simulator` source so the recipes run anywhere;
substitute your real source and its `--opt` settings (see `battfeed sources`
for what is installed and available on the host).

## The pattern: bounded runs under a supervisor

Prefer **bounded runs restarted by the service manager** over one eternal
process:

```sh
battfeed collect --source simulator --duration 3600 --interval 1 \
    --institution LOCAL --cell cell01
```

- Each run writes one `INSTITUTION__CELL__YYYYMMDD_NNN.bdf.csv` plus its
  `.meta.json` sidecar into the **working directory**, then exits 0; the
  supervisor starts the next run, which picks the next free `_NNN` sequence
  number. You get natural data-file rotation, and every completed hour is a
  finished, shippable file.
- The sequence number is capped at `999` per institution/cell/day, so keep
  `--duration` at 90 seconds or more for 24/7 operation (86400 s / 999 ≈ 87 s
  per file).
- Do **not** pass a fixed `--out` path to a restarting service: the sink opens
  its output in write mode, so every restart would overwrite the same file.
  Let the default generated names do the rotation.
- Exit codes: `0` = duration elapsed or interrupted cleanly; `1` = the source
  failed (its error budget was exhausted); `2` = bad source name or options.
  Exit 2 is a configuration mistake that restarting will not fix — always run
  the exact command interactively once before installing it as a service.

## What `finalized` in the sidecar means

Next to every data file battfeed writes a `<name>.meta.json` sidecar. It is
written **early** (as soon as the data file is opened, with
`"finalized": false`), refreshed roughly every 60 seconds during collection,
and rewritten one final time with `"finalized": true` and the final row count
on every clean exit — including Ctrl-C/SIGINT and source-failure exits.

So on disk:

- `"finalized": true` — the run ended under program control; the pair is
  complete and safe to ship.
- `"finalized": false` — the process is either *still running* or was killed
  hard (crash, `kill -9`, power loss, Task Scheduler "End task"). Rows are
  flushed after every poll, so the `.bdf.csv` data itself is still valid up to
  roughly the last sample; only the "this file is done" marker is missing.

Downstream movers/importers should select files by sidecar: **only pick up
pairs whose `.meta.json` has `"finalized": true`**, and treat unfinalized
sidecars older than a couple of poll intervals as crash leftovers to triage.

## Where files live

battfeed keeps **no state other than the output files**: no database, ledger,
or spool directory. The `.bdf.csv` + `.meta.json` pairs in the working
directory (or wherever `--out` points) are the entire on-disk record of what
has been collected. Two consequences:

- Set the service's working directory deliberately (`WorkingDirectory=` in
  systemd, `-WorkingDirectory`/`AppDirectory` on Windows) to a dedicated data
  directory, e.g. `/var/lib/battfeed` or `C:\battfeed\data`.
- "What has been collected" is answered by listing that directory; freeing
  space or shipping data is moving finalized pairs out of it.

## Graceful shutdown is SIGINT (Ctrl-C), not SIGTERM

`battfeed collect` installs a SIGINT handler that stops the loop and
finalizes the sidecar. **SIGTERM is not handled** — it kills the process
immediately and leaves `"finalized": false`. Configure supervisors
accordingly:

- systemd: set `KillSignal=SIGINT` (default is SIGTERM).
- NSSM: the default stop sequence sends a console Ctrl-C first — graceful out
  of the box.
- Windows Task Scheduler: "End task" / "Stop the task if it runs longer than"
  are hard kills. Prefer `--duration` to end runs, not the scheduler's stop.

## Linux: systemd

`/etc/systemd/system/battfeed-collect.service`:

```ini
[Unit]
Description=battfeed collector
# For network-backed sources only; harmless otherwise:
Wants=network-online.target
After=network-online.target

[Service]
Type=exec
User=battfeed
WorkingDirectory=/var/lib/battfeed
ExecStart=/opt/battfeed/venv/bin/battfeed -v collect \
    --source simulator --duration 3600 --interval 1 \
    --institution LOCAL --cell cell01
# Bounded runs exit 0; restart after success AND failure:
Restart=always
RestartSec=5
# battfeed finalizes its output on SIGINT (Ctrl-C), not SIGTERM:
KillSignal=SIGINT
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

Install and run:

```sh
sudo useradd --system --home-dir /var/lib/battfeed --create-home battfeed
sudo python3 -m venv /opt/battfeed/venv
sudo /opt/battfeed/venv/bin/pip install battfeed        # add extras as needed
sudo systemctl daemon-reload
sudo systemctl enable --now battfeed-collect
journalctl -u battfeed-collect -f
```

Notes:

- `Restart=always` (not `on-failure`) because a completed bounded run exits 0
  and must still be relaunched. systemd's default start-limit (5 starts in
  10 s) will stop a service that dies instantly every time — which is exactly
  what you want for exit-code-2 configuration mistakes.
- Hardware access: add the service user to the relevant groups (`dialout` for
  serial, `bluetooth`/BlueZ D-Bus access for `battfeed[mc3000-ble]`, a udev
  rule for raw USB with `battfeed[mc3000-usb]`, and `adb` needs the user to
  own the adb server for the `android` source).

## Windows: Task Scheduler

Task Scheduler discards a console program's stdout/stderr, so run battfeed
through `cmd.exe` with redirection appended to a per-day log file. Register
the task from an elevated PowerShell:

```powershell
$exe  = 'C:\battfeed\venv\Scripts\battfeed.exe'
$args = 'collect --source simulator --duration 3540 --interval 1 --institution LOCAL --cell cell01'

$action = New-ScheduledTaskAction -Execute 'cmd.exe' `
    -Argument ('/c ""{0}" -v {1} >> "C:\battfeed\logs\battfeed-%DATE:/=-%.log" 2>&1"' -f $exe, $args) `
    -WorkingDirectory 'C:\battfeed\data'

# Start one minute from now, then repeat every hour, forever.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration ([TimeSpan]::MaxValue)

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -StartWhenAvailable

Register-ScheduledTask -TaskName 'battfeed-collect' `
    -Action $action -Trigger $trigger -Settings $settings `
    -User 'SYSTEM' -RunLevel Highest
```

How the pieces fit:

- `--duration 3540` (59 min) under an hourly repetition leaves a gap so runs
  never overlap; `-MultipleInstances IgnoreNew` is the backstop if one does.
- The task runs as `SYSTEM` so it needs no logged-on user. Sources that talk
  to per-user services (e.g. `adb`) may need a real service account instead.
- `-ExecutionTimeLimit` is a hard kill (sidecar left unfinalized) — it is a
  safety net, not the normal end of a run; `--duration` is.
- Create `C:\battfeed\data` and `C:\battfeed\logs` before first run.

## Windows: NSSM (restart-on-crash service)

[NSSM](https://nssm.cc/) wraps battfeed as a real Windows service with
restart-on-exit and built-in log rotation, and its default stop method sends
Ctrl-C — so stops finalize the sidecar, unlike Task Scheduler's "End task".
From an elevated prompt:

```bat
nssm install battfeed "C:\battfeed\venv\Scripts\battfeed.exe" -v collect --source simulator --duration 3600 --interval 1 --institution LOCAL --cell cell01
nssm set battfeed AppDirectory C:\battfeed\data
nssm set battfeed AppStdout C:\battfeed\logs\battfeed.log
nssm set battfeed AppStderr C:\battfeed\logs\battfeed.log
nssm set battfeed AppRotateFiles 1
nssm set battfeed AppRotateOnline 1
nssm set battfeed AppRotateBytes 10485760
nssm set battfeed AppExit Default Restart
nssm set battfeed AppRestartDelay 5000
nssm start battfeed
```

`AppExit Default Restart` relaunches after every exit (bounded runs and
crashes alike), mirroring the systemd `Restart=always` recipe.

## Machine-sleep caveats

- Sleep/hibernate pauses the process. There is no catch-up on resume — the
  data simply has a gap — and device-backed sources (BLE, USB, adb, serial)
  frequently come back broken after resume, exhaust their error budget, and
  exit 1. The supervisor restart is the designed recovery path, but the
  cleaner answer is to not sleep at all on a collector host:
  - Linux: `sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`
  - Windows: `powercfg /change standby-timeout-ac 0` and
    `powercfg /change hibernate-timeout-ac 0` (check lid-close action on
    laptops).
- Task Scheduler's "Wake the computer to run this task" wakes the machine for
  the trigger but does not stop it sleeping again mid-run; it is not a
  substitute for the power settings above.

## Log rotation

- battfeed writes its **logs to stderr** only (`-v` enables INFO level; the
  data never goes to stdout/stderr). Under systemd, journald captures and
  rotates them — nothing to configure. Under NSSM, use the `AppRotate*`
  settings shown above. Under Task Scheduler, the redirection recipe above
  starts a new log file per day; delete old ones with your existing cleanup
  tooling.
- **Data files** rotate by design through bounded `--duration` runs — there is
  nothing to truncate. Archive or ship pairs whose sidecar says
  `"finalized": true`; never move a pair while its sidecar is unfinalized and
  fresh (that is the live run).
