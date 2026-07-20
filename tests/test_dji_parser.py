"""dji-log wrapper: file classification, command building, failure surfacing.

Ported from the donor test suite. No test here executes the real dji-log
binary: subprocess is faked, and the one real-subprocess path lives in
test_dji_source.py behind a generated fake executable.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from battfeed.sources.dji import parser
from battfeed.sources.dji.parser import (
    ParseError,
    UnsupportedFormat,
    build_command,
    classify,
    find_binary,
    parse_flight,
)


def test_classify():
    assert classify(Path("DJIFlightRecord_2023-08-06_[12-18-56].txt")) == "txt"
    assert classify(Path("FlightRecord_2026-06-16_[19-15-06].txt")) == "txt"
    assert classify(Path("2022-10-30_12-26-41_FLY019.DAT")) == "dat"
    assert classify(Path("DJIPlaybackDatabase.sqlite")) == "unknown"


def test_dat_and_unknown_are_unsupported(tmp_path):
    dat = tmp_path / "FLY019.DAT"
    dat.write_bytes(b"\x00" * 16)
    with pytest.raises(UnsupportedFormat):
        parse_flight(dat, tmp_path / "out.csv")
    other = tmp_path / "image.jpg"
    other.write_bytes(b"\xff\xd8")
    with pytest.raises(UnsupportedFormat):
        parse_flight(other, tmp_path / "out.csv")


def test_missing_binary_is_retryable(tmp_path, monkeypatch):
    monkeypatch.delenv("DJI_LOG_BIN", raising=False)
    monkeypatch.setattr(parser.shutil, "which", lambda name: None)
    monkeypatch.setattr(parser.Path, "is_file", lambda self: False)
    raw = tmp_path / "FlightRecord_x.txt"
    raw.write_text("stub")
    with pytest.raises(ParseError, match="dji-log binary not found"):
        parse_flight(raw, tmp_path / "out.csv")


def test_find_binary_prefers_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("DJI_LOG_BIN", raising=False)
    real = tmp_path / "dji-log"
    real.write_text("stub")
    assert find_binary(str(real)) == str(real)
    assert find_binary(str(tmp_path / "missing")) is None


def test_build_command_with_key_and_kml(tmp_path):
    raw, out = tmp_path / "r.txt", tmp_path / "o.csv"
    cmd = build_command("dji-log", raw, out, api_key="SECRET", emit_kml=True)
    assert cmd[:3] == ["dji-log", "-c", str(out)]
    assert cmd[cmd.index("-a") + 1] == "SECRET"
    assert cmd[cmd.index("-k") + 1] == str(out.with_suffix(".kml"))
    # SECURITY: the input path is the last arg, right after a "--" terminator,
    # and is absolutized (rooted, never dash-leading).
    assert cmd[-2] == "--"
    assert Path(cmd[-1]).is_absolute()
    assert Path(cmd[-1]) == raw.resolve()


def test_build_command_roots_relative_dash_named_file(monkeypatch, tmp_path):
    """A file named like an option, in a relative dir, cannot reach dji-log in
    flag position: the path is rooted and sits after '--' (mirrors attack1)."""
    monkeypatch.chdir(tmp_path)
    raw = Path("--csv=PWNED.txt")  # relative, dash-leading, option-looking
    cmd = build_command("dji-log", raw, Path("out.csv"), None, False)
    assert cmd[-2] == "--"
    path_arg = cmd[-1]
    assert Path(path_arg).is_absolute()
    assert not path_arg.startswith("-")  # never a bare dash-leading option
    assert cmd.index("--") == len(cmd) - 2  # nothing but the path follows "--"


def test_api_key_is_redacted_in_logs(tmp_path, raw_txt, monkeypatch, caplog):
    monkeypatch.setattr(parser, "resolve_binary", lambda configured=None: "dji-log")

    def write_csv(cmd):
        Path(cmd[cmd.index("-c") + 1]).write_text("col1,col2\n1,2\n")

    monkeypatch.setattr(parser.subprocess, "run", _fake_run(0, side_effect=write_csv))
    import logging

    with caplog.at_level(logging.INFO, logger="battfeed.sources.dji.parser"):
        parse_flight(raw_txt, tmp_path / "out.csv", api_key="SUPERSECRET")
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "SUPERSECRET" not in joined
    assert "***" in joined


def _fake_run(returncode: int, stderr: str = "", side_effect=None):
    def run(cmd, capture_output, text, creationflags=0):
        if side_effect:
            side_effect(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)

    return run


@pytest.fixture
def raw_txt(tmp_path):
    raw = tmp_path / "FlightRecord_2026-06-16_[19-15-06].txt"
    raw.write_text("stub record")
    return raw


def test_encrypted_record_hint_mentions_network(tmp_path, raw_txt, monkeypatch):
    monkeypatch.setattr(parser, "resolve_binary", lambda configured=None: "dji-log")
    monkeypatch.setattr(
        parser.subprocess,
        "run",
        _fake_run(101, stderr="thread 'main' panicked: API Key is required"),
    )
    with pytest.raises(ParseError, match="DJI_API_KEY") as excinfo:
        parse_flight(raw_txt, tmp_path / "out.csv")
    assert "network" in str(excinfo.value).lower()  # decryption is not offline


def test_truncated_record_is_unsupported_not_retried(tmp_path, raw_txt, monkeypatch):
    monkeypatch.setattr(parser, "resolve_binary", lambda configured=None: "dji-log")
    monkeypatch.setattr(
        parser.subprocess,
        "run",
        _fake_run(
            101, stderr='Error { kind: UnexpectedEof, message: "failed to fill whole buffer" }'
        ),
    )
    with pytest.raises(UnsupportedFormat, match="truncated or corrupt"):
        parse_flight(raw_txt, tmp_path / "out.csv")


def test_empty_output_is_failure(tmp_path, raw_txt, monkeypatch):
    monkeypatch.setattr(parser, "resolve_binary", lambda configured=None: "dji-log")

    def write_header_only(cmd):
        Path(cmd[cmd.index("-c") + 1]).write_text("col1,col2\n")

    monkeypatch.setattr(parser.subprocess, "run", _fake_run(0, side_effect=write_header_only))
    with pytest.raises(ParseError, match="no data rows"):
        parse_flight(raw_txt, tmp_path / "out.csv")


def test_success(tmp_path, raw_txt, monkeypatch):
    monkeypatch.setattr(parser, "resolve_binary", lambda configured=None: "dji-log")

    def write_csv(cmd):
        Path(cmd[cmd.index("-c") + 1]).write_text("col1,col2\n1,2\n")

    monkeypatch.setattr(parser.subprocess, "run", _fake_run(0, side_effect=write_csv))
    out = parse_flight(raw_txt, tmp_path / "out.csv")
    assert out.read_text().count("\n") == 2
