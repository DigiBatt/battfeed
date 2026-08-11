# Validate output against the BDF reference

battfeed emits conforming BDF by construction, but "check, don't trust" is cheap: the `bdf` extra wraps the reference implementation's checker so you can prove a file conforms.

```bash
pip install "battfeed[bdf]"
```

```python
>>> from battfeed.sinks.bdf_csv import validate_file
>>> validate_file("LOCAL__DemoCell__20260811_001.bdf.csv")
{'ok': True, 'missing': [], 'extras': ['surface_temperature_celsius'], ...}
```

The report tells you whether the file is acceptable (`ok`), which required columns are missing if not, and which columns are extras beyond the required trio — extras are legitimate, the field exists so you can spot typos (`surface_temp_celsius` would show up here instead of matching an optional column).

## When to run it

- **In your source's test suite**, on a file collected from your source via a short `Harvester` run — this catches column-name mistakes `check_source` cannot see (it checks samples, not files).
- **Spot-checking a deployment**, especially after changing a `csvtail` column map or unit scale.
- There is no need to validate every production file in-line; conformance is a property of the writing code, not of individual runs.

## What "conforming" covers

Header naming and ordering (`test_time_second, voltage_volt, current_ampere` first, extras alphabetical), snake_case `{quantity}_{unit}` column names, and the sign convention are battfeed's responsibility and specified in the [output contract](../reference/output.md). Semantic plausibility of the *values* (a 40 V coin cell) is yours; battfeed records what the source reports.
