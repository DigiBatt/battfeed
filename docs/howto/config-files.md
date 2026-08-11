# Use config files (and keep secrets out of sight)

A long invocation — an mc3000 slot and BLE address, a dji folder and keychain key — can live in a TOML file instead of a wall of `--opt` flags.

## The file

```toml
[collect]                        # defaults for `battfeed collect`
interval = 2.0
institution = "SINTEF"
cell = "Pack-07"

[import]                         # defaults for `battfeed import`
out_dir = "imported"
watch = true

[source.mc3000]                  # options for `--source mc3000`
slot = 1
transport = "ble"
address = "AA:BB:CC:DD:EE:FF"

[source.dji]                     # options for `--source dji`
path = "C:/logs/dji"
api_key = "${ENV:DJI_API_KEY}"   # expanded from the environment at load time
```

Pass it with `--config` on `collect` or `import`. A real run, with the source options and run parameters coming from the file:

```console
$ battfeed collect --source simulator --config lab.toml --duration 6
Collected 3 sample(s) from 'simulator' in 6.0 s -> SINTEF__Bench-01__20260811_001.bdf.csv
```

A `[source.<name>]` block matches `--source <name>`; its optional `type` field selects the registered source (defaulting to the block name, so a block can alias one source under another name). **Precedence**, highest first:

- source options: `--opt KEY=VALUE` > `[source.<name>]` > the source's own default
- run parameters: the CLI flag > `[collect]` / `[import]` > the built-in default

`--config` uses `tomllib`, standard library on Python 3.11+; on 3.10 install the `tomli` backport — no dependency is added to the core.

## Secrets

Passing a token as `--opt api_key=...` leaks it into shell history and the process list (`ps` and Task Manager show full command lines). Prefer the config file with **`${ENV:VAR}` expansion**, so the secret lives only in the environment and never touches the file:

```toml
[source.push]
token = "${ENV:INGEST_TOKEN}"
```

Whatever the entry path, battfeed redacts credentials in two complementary layers before they can reach a log, an error message, the `battfeed sources` listing, or a `.meta.json` sidecar:

- **by key name** — a value whose key looks like a credential (`*key*`, `*token*`, `*secret*`, `*password*`, `*auth*`, ...) is masked to `***`;
- **by value** — the concrete secret values resolved for a run, plus any `scheme://user:pass@host` URL userinfo, are scrubbed even when they hide under an innocuous key.

The helpers are public for source authors: `battfeed.config.redact_mapping`, `redact_text`, `is_secret_key`.
