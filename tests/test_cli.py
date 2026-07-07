from __future__ import annotations

import json

from battfeed import cli
from battfeed.sinks.bdf_csv import REQUIRED_COLUMNS


def test_sources_subcommand_lists_builtins(capsys):
    assert cli.main(["sources"]) == 0
    out = capsys.readouterr().out
    for name in ("simulator", "csvtail", "wmi"):
        assert name in out


def test_collect_simulator_end_to_end(tmp_path, capsys):
    out_path = tmp_path / "TEST__SimCell__20260707_001.bdf.csv"
    exit_code = cli.main(
        [
            "collect",
            "--source",
            "simulator",
            "--duration",
            "0.05",
            "--interval",
            "0.01",
            "--out",
            str(out_path),
            "--institution",
            "TEST",
            "--cell",
            "SimCell",
        ]
    )
    assert exit_code == 0

    lines = out_path.read_text(encoding="utf-8").splitlines()
    assert lines[0].split(",")[:3] == list(REQUIRED_COLUMNS)
    assert len(lines) >= 2  # header plus at least one sample

    sidecar = json.loads(
        (tmp_path / "TEST__SimCell__20260707_001.meta.json").read_text(encoding="utf-8")
    )
    assert sidecar["metadata"]["institution"] == "TEST"
    assert sidecar["metadata"]["source"]["source"] == "simulator"

    summary = capsys.readouterr().out
    assert "simulator" in summary and str(out_path) in summary


def test_collect_unknown_source_fails_cleanly(capsys):
    assert cli.main(["collect", "--source", "nope", "--duration", "0.01"]) == 2
    assert "Unknown source" in capsys.readouterr().err
