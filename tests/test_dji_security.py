"""Security + timebase regressions for the DJI source (red-team hardening).

Each test mirrors one red-team attack from the WP1.4 review:
  * MAJOR-1 arg injection  -> attack1_arginject.py
  * MAJOR-2 API-key leak    -> attack2_keyleak.py
  * MAJOR-3 non-finite NaN  -> attack4_content.py (nan/inf rows)
  * MAJOR-4 poison-pill DoS -> attack4b_poison.py
  * MINOR-5/6 timebase      -> attack567.py (7.x) + attack8_checksource.py
  * MINOR-7 serial collision-> attack567.py (6.1)
  * NIT-9 quarantine spam   -> attack567.py (5.1)
"""

from __future__ import annotations

import json
import logging
import math
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from battfeed import ErrorPolicy, run_import
from battfeed.sources.dji import parser
from battfeed.sources.dji import source as source_mod
from battfeed.sources.dji.gate import normalize_flight
from battfeed.sources.dji.source import DjiFlightLogSource
from battfeed.testing import check_source

HASH = "abc123def456abc123def456"


class RowsSink:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def write(self, rows) -> None:
        self.rows.extend(dict(r) for r in rows)

    def close(self) -> None:
        pass


def _copy_parse(raw: Path, out_csv: Path, **kwargs) -> Path:
    """Fake dji-log: .DAT -> unsupported, else copy the raw CSV bytes out."""
    if raw.name.lower().endswith(".dat"):
        raise source_mod.UnsupportedFormat(f"{raw.name}: .DAT not supported")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    out_csv.write_bytes(raw.read_bytes())
    return out_csv


# -- MAJOR-1: argument injection ---------------------------------------------


def _write_argv_capturing_binary(tmp_path: Path) -> Path:
    """A fake dji-log that records the exact argv it receives (via FAKE_ARGV_OUT)."""
    fake_py = tmp_path / "fake.py"
    fake_py.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "argv = sys.argv[1:]\n"
        "Path(os.environ['FAKE_ARGV_OUT']).write_text(json.dumps(argv), encoding='utf-8')\n"
        "out = Path(argv[argv.index('-c') + 1])\n"
        "out.write_text('CUSTOM.dateTime,BATTERY.voltage,BATTERY.current\\n"
        "2026-06-17T07:31:27Z,12.0,5.0\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    if sys.platform == "win32":
        launcher = tmp_path / "dji-log.cmd"
        launcher.write_text(f'@"{sys.executable}" "{fake_py}" %*\n', encoding="utf-8")
    else:
        launcher = tmp_path / "dji-log"
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{fake_py}" "$@"\n', encoding="utf-8"
        )
        launcher.chmod(0o755)
    return launcher


def test_arg_injection_dji_log_receives_rooted_path_after_double_dash(tmp_path, monkeypatch):
    """A file named like an option reaches dji-log ONLY as a rooted positional
    after '--' -- never in flag position (mirrors attack1)."""
    launcher = _write_argv_capturing_binary(tmp_path)
    capture = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_ARGV_OUT", str(capture))
    data = tmp_path / "records"
    data.mkdir()
    (data / "--csv=PWNED.txt").write_bytes(b"raw record bytes")  # attacker filename

    src = DjiFlightLogSource(data, dji_log_bin=str(launcher))
    src.poll()

    argv = json.loads(capture.read_text(encoding="utf-8"))
    assert "--" in argv
    path_arg = argv[-1]
    assert argv[argv.index("--") + 1] == path_arg  # path immediately follows "--"
    assert Path(path_arg).is_absolute()  # rooted
    assert not path_arg.startswith("-")  # never a bare dash-leading option
    assert path_arg.endswith("PWNED.txt")


# -- MAJOR-2: API-key leak ---------------------------------------------------


def test_api_key_never_leaks_into_exception_or_logs(tmp_path, monkeypatch, caplog):
    """dji-log reflecting the key in stderr must not leak it into the raised
    ParseError or any log record (mirrors attack2)."""
    secret = "SK-LIVE-abc123-SUPERSECRET-KEY"
    raw = tmp_path / "FlightRecord_leak.txt"
    raw.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(parser, "resolve_binary", lambda configured=None: "dji-log")

    def fake_run(cmd, capture_output, text, creationflags=0):
        key = cmd[cmd.index("-a") + 1] if "-a" in cmd else "<none>"
        return subprocess.CompletedProcess(
            cmd, 2, stdout="", stderr=f"error: invalid keychain response for API Key {key}"
        )

    monkeypatch.setattr(parser.subprocess, "run", fake_run)
    with caplog.at_level(logging.INFO, logger="battfeed.sources.dji.parser"):
        with pytest.raises(parser.ParseError) as excinfo:
            parser.parse_flight(raw, tmp_path / "out.csv", api_key=secret)

    assert secret not in str(excinfo.value)
    assert "***" in str(excinfo.value)
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in blob and "SUPERSECRET" not in blob


# -- MAJOR-3: non-finite (NaN / Infinity) ------------------------------------


def test_non_finite_values_are_dropped_in_every_field(tmp_path):
    hdr = "CUSTOM.dateTime,BATTERY.voltage,BATTERY.current\n"
    body = (
        "2026-06-17T07:31:00Z,12.0,NaN\n"  # NaN current
        "2026-06-17T07:31:01Z,Infinity,5.0\n"  # +inf voltage
        "2026-06-17T07:31:02Z,-inf,5.0\n"  # -inf voltage
        "2026-06-17T07:31:03Z,12.0,5.0\n"  # the only finite row
    )
    path = tmp_path / "f.csv"
    path.write_text(hdr + body, encoding="utf-8")
    fd = normalize_flight(path, HASH)
    assert len(fd.rows) == 1
    assert fd.meta["dropped"]["implausible"] == 3  # each non-finite row counted
    for row in fd.rows:
        for value in row.values():
            if isinstance(value, float):
                assert math.isfinite(value)
    blob = json.dumps(fd.rows).lower()
    assert "nan" not in blob and "inf" not in blob


# -- MAJOR-4: poison-pill DoS ------------------------------------------------


def test_poison_pill_is_quarantined_and_later_file_still_imports(tmp_path, monkeypatch):
    """One pathological dji-log CSV is quarantined (not retried to SourceFailure)
    and a good file after it still imports (mirrors attack4b)."""
    big = "A" * 200_000  # exceeds csv's default field_size_limit (131072)

    def parse(raw: Path, out_csv: Path, **kwargs) -> Path:
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        if "poison" in raw.name:
            out_csv.write_text(
                "CUSTOM.dateTime,BATTERY.voltage,BATTERY.current,RECOVER.aircraftName\n"
                f"2026-06-17T07:31:27Z,12.0,5.0,{big}\n",
                encoding="utf-8",
            )
        else:
            out_csv.write_text(
                "CUSTOM.dateTime,BATTERY.voltage,BATTERY.current\n2026-06-17T07:31:27Z,12.0,5.0\n",
                encoding="utf-8",
            )
        return out_csv

    monkeypatch.setattr(source_mod, "parse_flight", parse)
    data = tmp_path / "records"
    data.mkdir()
    # "1poison" sorts BEFORE "2good", so the poison file is hit first.
    (data / "FlightRecord_1poison.txt").write_text("raw", encoding="utf-8")
    (data / "FlightRecord_2good.txt").write_text("raw2", encoding="utf-8")

    sink = RowsSink()
    stats = run_import(
        DjiFlightLogSource(data),
        sink,
        stop=threading.Event(),
        errors=ErrorPolicy(max_consecutive_errors=3),
        clock=lambda: 0.0,
        sleep=lambda s: None,
    )
    assert stats.samples == 1  # the good file imported despite the poison file

    src = DjiFlightLogSource(data)
    poison = data / "FlightRecord_1poison.txt"
    digest = src._ledger.hash_of(poison)
    assert src._ledger.is_quarantined(poison, content_hash=digest)
    reason = src._ledger.quarantine_reason(poison, content_hash=digest) or ""
    assert "malformed" in reason


# -- MINOR-5/6: zero-based, monotonic timebase -------------------------------


def test_leading_implausible_row_still_starts_at_zero(tmp_path):
    hdr = "CUSTOM.dateTime,BATTERY.voltage,BATTERY.current\n"
    body = (
        "2026-06-17T07:31:00Z,999.0,5.0\n"  # implausible pack voltage -> dropped
        "2026-06-17T07:31:10Z,12.0,5.0\n"  # first KEPT -> t=0
        "2026-06-17T07:31:20Z,11.9,6.0\n"
    )
    path = tmp_path / "f.csv"
    path.write_text(hdr + body, encoding="utf-8")
    fd = normalize_flight(path, HASH)
    assert [r["test_time_second"] for r in fd.rows] == [0.0, 10.0]


def test_out_of_order_rows_have_no_negative_time(tmp_path):
    hdr = "CUSTOM.dateTime,BATTERY.voltage,BATTERY.current\n"
    body = (
        "2026-06-17T07:31:20Z,11.9,6.0\n"  # later row first
        "2026-06-17T07:31:00Z,12.0,5.0\n"  # earlier row second
    )
    path = tmp_path / "f.csv"
    path.write_text(hdr + body, encoding="utf-8")
    fd = normalize_flight(path, HASH)
    tts = [r["test_time_second"] for r in fd.rows]
    assert tts == [0.0, 20.0]
    assert all(t >= 0 for t in tts)


def test_check_source_passes_on_adversarial_but_parseable_files(tmp_path, monkeypatch):
    """The same kit that FAILED the old source on NaN / out-of-order inputs now
    passes: the gate drops NaN and guarantees zero-based/monotone (mirrors attack8)."""
    monkeypatch.setattr(source_mod, "parse_flight", _copy_parse)
    hdr = (
        "CUSTOM.dateTime,BATTERY.voltage,BATTERY.current,"
        "BATTERY.cellNum,BATTERY.cellVoltage1,BATTERY.cellVoltage2,BATTERY.cellVoltage3\n"
    )
    nan_dir = tmp_path / "nan"
    nan_dir.mkdir()
    (nan_dir / "flight.txt").write_text(
        hdr
        + "2026-06-17T07:31:27Z,12.0,NaN,3,4.0,4.0,4.0\n"  # dropped
        + "2026-06-17T07:31:37Z,12.0,5.0,3,4.0,4.0,4.0\n",  # kept, finite
        encoding="utf-8",
    )
    check_source(DjiFlightLogSource(nan_dir, ledger_path=nan_dir / ".l.json"), polls=1)

    neg_dir = tmp_path / "neg"
    neg_dir.mkdir()
    (neg_dir / "flight.txt").write_text(
        hdr
        + "2026-06-17T07:31:37Z,12.0,5.0,3,4.0,4.0,4.0\n"  # out of order
        + "2026-06-17T07:31:27Z,12.0,5.0,3,4.0,4.0,4.0\n",
        encoding="utf-8",
    )
    check_source(DjiFlightLogSource(neg_dir, ledger_path=neg_dir / ".l.json"), polls=1)


# -- MINOR-7: serial-fallback collision --------------------------------------


def test_two_serial_less_files_get_distinct_series_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(source_mod, "parse_flight", _copy_parse)
    hdr = (
        "CUSTOM.dateTime,BATTERY.voltage,BATTERY.current,"
        "RECOVER.aircraftSerial,RECOVER.batterySerial\n"
    )
    data = tmp_path / "d"
    data.mkdir()
    (data / "packA.txt").write_text(hdr + "2026-06-17T07:31:27Z,12.0,5.0,,\n", encoding="utf-8")
    (data / "packB.txt").write_text(hdr + "2026-06-18T08:00:00Z,11.5,4.0,,\n", encoding="utf-8")
    src = DjiFlightLogSource(data, ledger_path=data / ".l.json")
    series = []
    for _ in range(2):
        rows = src.poll()
        if rows:
            series.append(rows[0]["series_id"])
            src.commit_batch()
    assert len(series) == 2
    assert len(set(series)) == 2  # distinct anonymous packs never merge


def test_identical_serials_share_series_across_files(tmp_path, monkeypatch):
    monkeypatch.setattr(source_mod, "parse_flight", _copy_parse)
    hdr = (
        "CUSTOM.dateTime,BATTERY.voltage,BATTERY.current,"
        "RECOVER.aircraftSerial,RECOVER.batterySerial\n"
    )
    data = tmp_path / "d"
    data.mkdir()
    (data / "f1.txt").write_text(hdr + "2026-06-17T07:31:27Z,12.0,5.0,AC,BAT\n", encoding="utf-8")
    (data / "f2.txt").write_text(hdr + "2026-06-18T09:00:00Z,11.0,4.0,AC,BAT\n", encoding="utf-8")
    src = DjiFlightLogSource(data, ledger_path=data / ".l.json")
    seen = []
    for _ in range(2):
        rows = src.poll()
        if rows:
            seen.append((rows[0]["series_id"], rows[0]["run_id"]))
            src.commit_batch()
    (s1, r1), (s2, r2) = seen
    assert s1 == s2 == "AC:BAT"  # one pack
    assert r1 != r2  # two flights


# -- NIT-9: quarantine log spam ----------------------------------------------


def test_quarantined_dat_warns_once_without_per_poll_skip_spam(tmp_path, monkeypatch):
    monkeypatch.setattr(source_mod, "parse_flight", _copy_parse)
    data = tmp_path / "d"
    data.mkdir()
    (data / "FLY001.DAT").write_bytes(b"aircraft dat bytes")

    logs: list[tuple[str, str]] = []
    handler = logging.Handler()
    handler.emit = lambda record: logs.append((record.levelname, record.getMessage()))
    loggers = ("battfeed.ingest_state", "battfeed.sources.dji.source")
    for name in loggers:
        logging.getLogger(name).addHandler(handler)
        logging.getLogger(name).setLevel(logging.INFO)
    try:
        src = DjiFlightLogSource(data, ledger_path=data / ".l.json")
        for _ in range(3):
            src.poll()
    finally:
        for name in loggers:
            logging.getLogger(name).removeHandler(handler)

    warns = [m for lv, m in logs if lv == "WARNING" and "Quarantined" in m]
    skips = [m for lv, m in logs if lv == "INFO" and "quarantined" in m.lower() and "Skipping" in m]
    assert len(warns) == 1  # the quarantine WARN fires exactly once
    assert skips == []  # the per-process cache prevents per-poll skip spam
