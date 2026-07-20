"""DjiFlightLogSource: ingestion, dedupe/quarantine, routing, CLI end-to-end.

No test executes the real dji-log binary except ``test_real_binary_path``,
which generates a tiny fake executable and points DJI_LOG_BIN at it. Every
other test injects a fake parser via the module-level ``parse_flight`` name.
"""

from __future__ import annotations

import csv
import json
import stat
import sys
from pathlib import Path

import pytest

from battfeed import cli
from battfeed.sources.dji import parser
from battfeed.sources.dji import source as source_mod
from battfeed.sources.dji.source import DjiFlightLogSource
from battfeed.testing import check_source

FIXTURES = Path(__file__).parent / "fixtures" / "dji"


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fake_parse_flight(raw: Path, out_csv: Path, **kwargs) -> Path:
    """Stand-in for dji-log: rejects .DAT, else copies the raw CSV bytes out.

    The dropped raw ``.txt`` files carry fixture CSV content directly, so the
    fake just materializes them at the requested ``-c`` path -- exactly what
    the real binary does, minus the decode.
    """
    if raw.name.lower().endswith(".dat"):
        raise source_mod.UnsupportedFormat(f"{raw.name}: aircraft .DAT not supported")
    if not raw.name.lower().endswith(".txt"):
        raise source_mod.UnsupportedFormat(f"{raw.name}: unrecognized file type")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    out_csv.write_bytes(raw.read_bytes())
    return out_csv


@pytest.fixture(autouse=True)
def _no_real_binary(monkeypatch):
    """Default every test to the fake parser; opt out where a real path is wanted."""
    monkeypatch.setattr(source_mod, "parse_flight", fake_parse_flight)


def drop(directory: Path, name: str, fixture: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    raw = directory / name
    raw.write_bytes(_fixture_bytes(fixture))
    return raw


# -- ingestion ---------------------------------------------------------------


def test_poll_ingests_one_flight_with_bdf_columns(tmp_path):
    drop(tmp_path, "pack_alpha.txt", "pack_alpha.csv")
    src = DjiFlightLogSource(tmp_path)
    rows = src.poll()
    # pack_alpha has an epoch head + corrupt tail; 3 plausible rows survive.
    assert len(rows) == 3
    first = rows[0]
    assert first["voltage_volt"] == 12.144
    assert first["current_ampere"] == -6.217  # negated
    assert first["test_time_second"] == 0.0
    assert first["series_id"] == "3N3BH6M0020165:1Z3PH69EA104UV"
    assert first["run_id"].startswith("flight-")
    assert "cell_1_voltage_volt" in first


def test_second_poll_of_same_dir_yields_nothing_after_commit(tmp_path):
    drop(tmp_path, "pack_alpha.txt", "pack_alpha.csv")
    src = DjiFlightLogSource(tmp_path)
    assert src.poll()  # ingest
    src.commit_batch()  # the driver's commit point
    assert src.poll() == []
    assert src.drained() is True


def test_no_commit_means_reimport(tmp_path):
    """Without commit_batch (crash before sink write), the file re-imports."""
    drop(tmp_path, "pack_alpha.txt", "pack_alpha.csv")
    src = DjiFlightLogSource(tmp_path)
    assert src.poll()
    # no commit_batch() -> ledger unchanged -> still pending work
    assert src.drained() is False
    assert src.poll()  # same file offered again


def test_dat_is_quarantined_once_and_remembered(tmp_path):
    dat = drop(tmp_path, "FLY019.DAT", "pack_alpha.csv")  # content irrelevant for .DAT
    src = DjiFlightLogSource(tmp_path)
    assert src.poll() == []  # quarantined, nothing to emit
    assert src.drained() is True
    digest = src._ledger.hash_of(dat)
    assert src._ledger.is_quarantined(dat, content_hash=digest)
    assert "not supported" in (src._ledger.quarantine_reason(dat, content_hash=digest) or "")
    # a copy with identical bytes is remembered, never re-processed
    drop(tmp_path, "FLY019_copy.DAT", "pack_alpha.csv")
    assert src.poll() == []


def test_all_implausible_file_is_quarantined_not_looped(tmp_path):
    # A CSV whose only row is bad-timestamp -> zero plausible rows.
    only_epoch = "CUSTOM.dateTime,BATTERY.voltage,BATTERY.current\n1970-01-01T00:00:00Z,12.0,5.0\n"
    raw = tmp_path / "empty_flight.txt"
    tmp_path.mkdir(parents=True, exist_ok=True)
    raw.write_text(only_epoch, encoding="utf-8")
    src = DjiFlightLogSource(tmp_path)
    assert src.poll() == []
    digest = src._ledger.hash_of(raw)
    assert src._ledger.is_quarantined(raw, content_hash=digest)
    assert src.drained() is True


def test_transient_parse_error_propagates(tmp_path, monkeypatch):
    drop(tmp_path, "pack_alpha.txt", "pack_alpha.csv")

    def boom(raw, out_csv, **kwargs):
        raise parser.ParseError("dji-log exit 101: API Key is required")

    monkeypatch.setattr(source_mod, "parse_flight", boom)
    src = DjiFlightLogSource(tmp_path)
    with pytest.raises(parser.ParseError):
        src.poll()
    # nothing recorded/quarantined -> retryable next run
    assert src.drained() is False


def test_zero_byte_file_is_ignored(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "empty.txt").write_bytes(b"")
    src = DjiFlightLogSource(tmp_path)
    assert src.poll() == []
    assert src.drained() is True  # zero-byte never counts as pending work


def test_single_file_path(tmp_path):
    raw = drop(tmp_path, "pack_bravo.txt", "pack_bravo.csv")
    src = DjiFlightLogSource(raw)  # a single file, not a directory
    rows = src.poll()
    assert len(rows) == 2
    assert rows[0]["series_id"] == "7X9AA1B2003377:2Q8RS55TT200ZZ"


def test_reset_ledger_reingests(tmp_path):
    drop(tmp_path, "pack_alpha.txt", "pack_alpha.csv")
    src = DjiFlightLogSource(tmp_path)
    src.poll()
    src.commit_batch()
    assert src.drained() is True
    src.reset_ledger()
    assert src.drained() is False
    assert src.poll()


def test_ledger_survives_new_instance(tmp_path):
    drop(tmp_path, "pack_alpha.txt", "pack_alpha.csv")
    first = DjiFlightLogSource(tmp_path)
    first.poll()
    first.commit_batch()
    second = DjiFlightLogSource(tmp_path)  # fresh instance, same ledger file
    assert second.drained() is True


# -- metadata ----------------------------------------------------------------


def test_metadata_declares_extension_and_caveats(tmp_path):
    drop(tmp_path, "pack_alpha.txt", "pack_alpha.csv")
    src = DjiFlightLogSource(tmp_path)
    src.poll()
    meta = src.metadata()
    assert meta["kind"] == "dji-flight-log"
    assert meta["extension_columns"] == [
        "cell_1_voltage_volt",
        "cell_2_voltage_volt",
        "cell_3_voltage_volt",
    ]
    assert "NEGATED" in meta["notes"] and "network" in meta["notes"].lower()
    assert meta["flights"][0]["records"] == 3
    # brand rule: provenance never names the donor product. The forbidden
    # tokens are assembled from fragments so the brand string never appears
    # literally anywhere in battfeed (source, test, or fixture).
    blob = json.dumps(meta).lower()
    brand = "battery"
    forbidden = [brand + ".tech", brand + "tech", brand + "-tech"]
    assert not any(token in blob for token in forbidden)
    assert "proprietary" in meta["notes"]  # the sanctioned provenance phrasing
    json.dumps(meta)  # JSON-serializable


# -- contract kit ------------------------------------------------------------


def test_check_source_passes(tmp_path):
    drop(tmp_path, "pack_alpha.txt", "pack_alpha.csv")
    check_source(DjiFlightLogSource(tmp_path))


# -- availability ------------------------------------------------------------


def test_availability_reports_missing_binary(monkeypatch):
    monkeypatch.delenv("DJI_LOG_BIN", raising=False)
    monkeypatch.setattr(source_mod, "find_binary", lambda configured=None: None)
    reason = DjiFlightLogSource.availability()
    assert reason is not None and "dji-log" in reason


def test_availability_ok_when_binary_present(monkeypatch, tmp_path):
    fake = tmp_path / "dji-log"
    fake.write_text("stub")
    monkeypatch.setenv("DJI_LOG_BIN", str(fake))
    assert DjiFlightLogSource.availability() is None


# -- CLI end-to-end ----------------------------------------------------------


def test_cli_import_end_to_end_one_file_per_pack_flight(tmp_path, capsys, monkeypatch):
    data = tmp_path / "records"
    out = tmp_path / "out"
    drop(data, "pack_alpha.txt", "pack_alpha.csv")
    drop(data, "pack_bravo.txt", "pack_bravo.csv")
    monkeypatch.setattr(source_mod, "parse_flight", fake_parse_flight)

    exit_code = cli.main(
        [
            "import",
            "--source",
            "dji",
            "--opt",
            f"path={data}",
            "--out-dir",
            str(out),
            "--institution",
            "TEST",
        ]
    )
    assert exit_code == 0

    files = sorted(out.glob("*.bdf.csv"))
    assert len(files) == 2  # one per (pack, flight)
    for path in files:
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        header = rows[0].keys()
        assert "series_id" not in header and "run_id" not in header
        assert list(header)[:3] == ["test_time_second", "voltage_volt", "current_ampere"]
        assert rows[0]["test_time_second"] == "0.0"
        sidecar = json.loads(
            path.with_name(path.name.removesuffix(".bdf.csv") + ".meta.json").read_text(
                encoding="utf-8"
            )
        )
        assert sidecar["finalized"] is True
        assert sidecar["metadata"]["series_id"].count(":") == 1
        assert sidecar["metadata"]["run_id"].startswith("flight-")
        # extension columns reach the sidecar through the source metadata block
        assert "cell_1_voltage_volt" in sidecar["metadata"]["source"]["extension_columns"]

    out_text = capsys.readouterr().out
    assert "Imported" in out_text and "2 file(s)" in out_text


def test_cli_import_is_idempotent(tmp_path, capsys, monkeypatch):
    data = tmp_path / "records"
    out = tmp_path / "out"
    drop(data, "pack_alpha.txt", "pack_alpha.csv")
    monkeypatch.setattr(source_mod, "parse_flight", fake_parse_flight)

    assert (
        cli.main(["import", "--source", "dji", "--opt", f"path={data}", "--out-dir", str(out)]) == 0
    )
    capsys.readouterr()
    # second run: ledger persists beside the data -> nothing new
    assert (
        cli.main(["import", "--source", "dji", "--opt", f"path={data}", "--out-dir", str(out)]) == 0
    )
    assert "Nothing to import" in capsys.readouterr().out
    assert len(list(out.glob("*.bdf.csv"))) == 1


# -- real subprocess path (generated fake executable, no real dji-log) -------


def _write_fake_binary(tmp_path: Path) -> Path:
    """Create a tiny executable that mimics dji-log: writes a valid CSV to -c."""
    fake_py = tmp_path / "fake_dji_log.py"
    fake_py.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "argv = sys.argv[1:]\n"
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
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return launcher


def test_real_binary_path(tmp_path, monkeypatch):
    """Exercise the true parse_flight subprocess path against a fake executable."""
    launcher = _write_fake_binary(tmp_path)
    monkeypatch.setattr(source_mod, "parse_flight", parser.parse_flight)  # undo autouse fake
    monkeypatch.setenv("DJI_LOG_BIN", str(launcher))
    assert DjiFlightLogSource.availability() is None

    data = tmp_path / "records"
    raw = data / "FlightRecord_real.txt"
    data.mkdir()
    raw.write_text("raw record bytes", encoding="utf-8")
    src = DjiFlightLogSource(data, dji_log_bin=str(launcher))
    rows = src.poll()
    assert len(rows) == 1
    assert rows[0]["voltage_volt"] == 12.0
    assert rows[0]["current_ampere"] == -5.0
    # fixture has no serials -> content-hash-derived fallback battery id
    assert rows[0]["series_id"].startswith("dji-drone:pack-")
