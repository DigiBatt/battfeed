"""Plausibility gate + BDF normalization for a parsed DJI flight CSV.

This is a **port** of a proprietary implementation (with the maintainer's
authorization): the plausibility logic is kept intact; only the *output
contract* is adapted -- the donor emitted device-agnostic ``telemetry_record``
dicts, whereas battfeed emits BDF (Battery Data Format) sample rows.

Unit/sign conventions (verified against real Mavic Air 2 logs):

* ``BATTERY.voltage`` is pack volts -> ``voltage_volt``.
* ``BATTERY.current`` is amps with **positive = draw FROM the pack**. The BDF
  sign convention is the opposite (**positive charges the test object**), so
  ``current_ampere`` is the **negation** of ``BATTERY.current``, and
  ``power_watt`` carries the same (negated) sign.
* ``BATTERY.temperature`` -> ``surface_temperature_celsius``.
* ``BATTERY.cellVoltageN`` -> per-cell ``cell_N_voltage_volt`` **extension**
  columns (not yet canonical BDF; declared in the source metadata so sidecars
  can carry ``extension_columns``).

Real logs end with corrupt tail rows (garbage voltages/temperatures after the
recorder loses sync) and begin with epoch-zero timestamps before GPS lock, so
every row passes a plausibility gate; dropped rows are counted per reason in
the returned flight summary rather than silently discarded (invariant I2).
Range checks alone miss tail rows whose fields are individually plausible but
mutually inconsistent, so rows that report per-cell voltages must also have the
pack voltage agree with the cell sum (verified: on real Mavic Air 2 logs they
agree to ~1 mV while decode garbage is off by volts).

One file = one flight = one run: every row carries ``run_id`` (``flight-<uid>``,
``<uid>`` = first 12 hex of the raw file's sha256) and ``series_id``
(``<aircraftSerial>:<batterySerial>``, with a content-hash-derived fallback for
a missing serial so distinct anonymous packs never merge). ``test_time_second``
(invariant I5) is zero-based per flight AND monotonic non-negative: the rows are
sorted by timestamp and the clock is based on the **earliest kept (plausible)**
row's timestamp, so a leading implausible row or out-of-order input can never
produce a negative or non-zero-based ``test_time_second``.

Non-finite guard: ``NaN`` / ``+/-Infinity`` in any numeric field are rejected at
parse (:func:`_f`) -- they are not valid BDF and, unguarded, would slip past
range checks (``abs(nan) > 200`` is ``False``) and reach an emitted row.
"""

from __future__ import annotations

import csv
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["FlightData", "cell_column", "normalize_flight", "stream_key"]

log = logging.getLogger(__name__)

# Plausibility bounds for field data. Per-cell voltage window is generous
# (LiPo storage to over-charged) -- the goal is to reject decode garbage
# (e.g. 51 V on a 3S pack, 3216 degC), not to police battery health.
_CELL_V_MIN, _CELL_V_MAX = 2.0, 4.6
_PACK_V_MIN, _PACK_V_MAX = 0.1, 60.0  # when cell count is unknown
_CURRENT_A_MAX = 200.0
_TEMP_C_MIN, _TEMP_C_MAX = -40.0, 100.0
_CELL_SUM_TOL_V = 0.5  # pack voltage vs sum of cell voltages
_MIN_YEAR = 2000  # pre-GPS-lock rows carry 1970-01-01

#: Fallback identity fields when the log never reports serials.
_DRONE_FALLBACK = "dji-drone"
_BATTERY_FALLBACK = "pack"


def _f(row: dict, key: str) -> float | None:
    v = row.get(key)
    if v is None or v == "":
        return None
    try:
        parsed = float(v)
    except ValueError:
        return None
    # Reject NaN / +/-Infinity in EVERY field: they are not valid BDF, they are
    # not JSON-representable, and unguarded they defeat the range checks
    # (abs(nan) > 200 is False) and reach an emitted row.
    return parsed if math.isfinite(parsed) else None


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.year < _MIN_YEAR:
        return None
    # Force tz-awareness (assume UTC, as DJI records are) so timestamps are
    # always mutually comparable/subtractable -- a mix of naive and aware rows
    # would otherwise raise when the flight is sorted by timestamp.
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def stream_key(aircraft: str, battery: str, pattern: str = "{aircraft}:{battery}") -> str:
    """Producer-side key identifying one battery pack flown on one aircraft."""
    return pattern.format(aircraft=aircraft, battery=battery)


def cell_column(index: int) -> str:
    """The BDF extension column name for the ``index``-th cell (1-based)."""
    return f"cell_{index}_voltage_volt"


def _plausible(
    row: dict,
    voltage: float,
    current: float,
    temperature: float | None,
    soc: float | None,
    cells: list[float],
) -> bool:
    cell_num = _f(row, "BATTERY.cellNum")
    if cell_num and 1 <= cell_num <= 14:
        lo, hi = cell_num * _CELL_V_MIN, cell_num * _CELL_V_MAX
    else:
        lo, hi = _PACK_V_MIN, _PACK_V_MAX
    if not (lo <= voltage <= hi):
        return False
    if abs(current) > _CURRENT_A_MAX:
        return False
    if temperature is not None and not (_TEMP_C_MIN <= temperature <= _TEMP_C_MAX):
        return False
    if soc is not None and not (0.0 <= soc <= 100.0):
        return False
    if any(not (0.0 <= c <= _CELL_V_MAX + 0.4) for c in cells):
        return False
    if cells and abs(voltage - sum(cells)) > _CELL_SUM_TOL_V:
        return False
    return True


def _gps(row: dict) -> tuple[float | None, float | None]:
    """Lat/lon if present and on Earth; corrupt rows carry values like -5e195."""
    lat, lon = _f(row, "OSD.latitude"), _f(row, "OSD.longitude")
    if lat is None or lon is None or not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None, None
    return lat, lon


def _cell_voltages(row: dict) -> list[float]:
    cells: list[float] = []
    for i in range(1, 15):
        v = _f(row, f"BATTERY.cellVoltage{i}")
        if v is None or v == 0.0:
            break
        cells.append(v)
    return cells


class FlightData:
    """The BDF rows of one flight plus its summary and extension-column list.

    Attributes:
        rows: BDF sample dicts, each carrying ``series_id`` / ``run_id`` and a
            zero-based-per-flight ``test_time_second`` (invariant I5).
        meta: A JSON-serializable flight summary (identity, row/drop counts,
            time window, SOC window, and flight context) -- for the sidecar and
            audit logging; nothing is silently dropped (invariant I2).
        extension_columns: The per-cell ``cell_N_voltage_volt`` columns present,
            declared so the sink metadata can carry ``extension_columns``.
    """

    def __init__(
        self, rows: list[dict[str, Any]], meta: dict[str, Any], extension_columns: list[str]
    ) -> None:
        self.rows = rows
        self.meta = meta
        self.extension_columns = extension_columns


def _bdf_row(
    row: dict,
    series_id: str,
    run_id: str,
    *,
    include_gps: bool,
    flight_context: dict[str, Any],
) -> dict[str, Any] | None:
    """Build one BDF sample from a CSV row, or ``None`` if the row is unusable.

    The plausibility gate below is the donor's, unchanged; only the returned
    mapping differs (BDF columns rather than the proprietary canonical record).
    ``test_time_second`` is **not** set here -- the caller stamps it after the
    flight's earliest kept timestamp is known (see :func:`normalize_flight`), so
    it is zero-based and monotonic. ``flight_context`` accumulates the
    drone-specific per-flight facts (GPS, height, motor state, capacity) that
    are NEVER BDF columns.
    """
    voltage = _f(row, "BATTERY.voltage")
    current = _f(row, "BATTERY.current")
    if voltage is None or current is None:
        return None
    temperature = _f(row, "BATTERY.temperature")
    soc = _f(row, "BATTERY.chargeLevel")
    cells = _cell_voltages(row)
    if not _plausible(row, voltage, current, temperature, soc, cells):
        return None

    current_a = -current  # DJI: positive = draw FROM pack; BDF: positive = charging
    sample: dict[str, Any] = {
        "voltage_volt": round(voltage, 6),
        "current_ampere": round(current_a, 6),
        "power_watt": round(voltage * current_a, 6),
        "series_id": series_id,
        "run_id": run_id,
    }
    if temperature is not None:
        sample["surface_temperature_celsius"] = round(temperature, 3)
    for i, cell_v in enumerate(cells, start=1):
        sample[cell_column(i)] = round(cell_v, 6)

    # Flight context -- accumulated on the summary, never emitted as columns.
    if soc is not None:
        flight_context.setdefault("_soc", []).append(soc)
    height = _f(row, "OSD.height")
    if height is not None:
        prev = flight_context.get("max_height_m")
        flight_context["max_height_m"] = height if prev is None else max(prev, height)
    if row.get("OSD.isMotorOn") == "true":
        flight_context["motor_on_rows"] = flight_context.get("motor_on_rows", 0) + 1
    remaining = _f(row, "BATTERY.currentCapacity")
    if remaining is not None:
        flight_context["remaining_capacity_mah"] = remaining
    full = _f(row, "BATTERY.fullCapacity")
    if full is not None:
        flight_context["full_capacity_mah"] = full
    if include_gps:
        lat, lon = _gps(row)
        if lat is not None and lon is not None:
            flight_context["last_latitude"] = lat
            flight_context["last_longitude"] = lon
    return sample


def normalize_flight(
    csv_path: Path,
    content_hash: str,
    *,
    include_gps: bool = False,
    source_file: str | None = None,
    drone_id: str = _DRONE_FALLBACK,
    stream_key_pattern: str = "{aircraft}:{battery}",
) -> FlightData:
    """Normalize one parsed flight CSV into BDF rows plus a flight summary.

    ``content_hash`` is the sha256 hex of the raw file (already computed once
    per poll by the import ledger); its first 12 characters form the
    ``run_id`` (``flight-<uid>``), so the raw file is hashed exactly once.
    """
    run_id = f"flight-{content_hash[:12]}"
    identity: dict[str, str | None] = {
        "aircraft_serial": None,
        "battery_serial": None,
        "drone_type": None,
        "aircraft_name": None,
    }
    parsed: list[tuple[datetime, dict]] = []
    dropped = {"bad_ts": 0, "implausible": 0}
    rows_total = 0

    with csv_path.open("r", newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            rows_total += 1
            # Identity fields can appear on any row; take the first non-empty.
            for meta_key, col in (
                ("aircraft_serial", "RECOVER.aircraftSerial"),
                ("battery_serial", "RECOVER.batterySerial"),
                ("drone_type", "OSD.droneType"),
                ("aircraft_name", "RECOVER.aircraftName"),
            ):
                if not identity[meta_key] and row.get(col):
                    identity[meta_key] = row[col].strip() or None
            ts = _parse_ts(row.get("CUSTOM.dateTime"))
            if ts is None:
                dropped["bad_ts"] += 1
                continue
            parsed.append((ts, row))

    # Zero-based AND monotonic: order the flight by timestamp so out-of-order
    # rows cannot yield a negative test_time_second.
    parsed.sort(key=lambda item: item[0])

    aircraft = identity["aircraft_serial"] or drone_id
    # Fold the file's content hash into a missing battery serial so two distinct
    # anonymous packs never collapse into one series_id (they differ by hash);
    # a real battery serial is used verbatim so a pack's flights still group.
    battery = identity["battery_serial"] or f"{_BATTERY_FALLBACK}-{content_hash[:12]}"
    series_id = stream_key(aircraft, battery, stream_key_pattern)

    kept: list[tuple[datetime, dict[str, Any]]] = []
    extension_columns: list[str] = []
    flight_context: dict[str, Any] = {}
    for ts, row in parsed:
        sample = _bdf_row(
            row,
            series_id,
            run_id,
            include_gps=include_gps,
            flight_context=flight_context,
        )
        if sample is None:
            dropped["implausible"] += 1
            continue
        for key in sample:
            if key.startswith("cell_") and key not in extension_columns:
                extension_columns.append(key)
        kept.append((ts, sample))

    # Base the clock on the earliest KEPT (plausible) row -- not merely the first
    # parsed one -- so a leading implausible row still yields a t=0 first sample.
    flight_start = kept[0][0] if kept else None
    rows: list[dict[str, Any]] = []
    for ts, sample in kept:
        assert flight_start is not None
        sample["test_time_second"] = round((ts - flight_start).total_seconds(), 6)
        rows.append(sample)

    socs = flight_context.pop("_soc", [])
    if flight_context.get("motor_on_rows") is not None and rows:
        flight_context["motor_on_fraction"] = round(
            flight_context.pop("motor_on_rows") / len(rows), 4
        )
    meta: dict[str, Any] = {
        "run_id": run_id,
        "series_id": series_id,
        "source_file": source_file,
        **identity,
        "rows_total": rows_total,
        "records": len(rows),
        "dropped": dropped,
        "test_time_span_second": rows[-1]["test_time_second"] if rows else None,
        "soc_start_pct": socs[0] if socs else None,
        "soc_end_pct": socs[-1] if socs else None,
        "extension_columns": list(extension_columns),
        "flight_context": {k: v for k, v in flight_context.items() if v is not None},
    }
    meta = {k: v for k, v in meta.items() if v is not None}
    if dropped["bad_ts"] or dropped["implausible"]:
        log.info(
            "Normalized %s: %d row(s) kept, dropped %d bad-timestamp + %d implausible",
            source_file or csv_path.name,
            len(rows),
            dropped["bad_ts"],
            dropped["implausible"],
        )
    return FlightData(rows, meta, extension_columns)
