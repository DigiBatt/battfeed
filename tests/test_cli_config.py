"""CLI ``--config`` precedence and credential-hygiene end-to-end tests.

The credential tests are the point of WP3.1: a secret handed to a run via
``--opt``, a config file, or ``${ENV:VAR}`` expansion must never surface on
disk (the ``.meta.json`` sidecar), in captured log output at ``--verbose``, or
in an error message from a failed run.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os

import pytest

from battfeed import cli
from battfeed.config import REDACTED

SECRET = "SK-LIVE-abc123-SUPERSECRET-KEY"


def _write_config(tmp_path, text: str):
    path = tmp_path / "battfeed.toml"
    path.write_text(text, encoding="utf-8")
    return path


@contextlib.contextmanager
def _capture_all_logs():
    """Capture fully-formatted records (message + rendered traceback) from root.

    The scrubbing filter is attached to this handler for the run, so what lands
    here is exactly what a real StreamHandler would emit -- including
    ``exc_text`` -- after scrubbing.
    """
    rendered: list[str] = []

    class Cap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            rendered.append(self.format(record))

    handler = Cap(level=logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    previous_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        yield rendered
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


# -- test doubles ------------------------------------------------------------


class RecordingSource:
    """Records the (type, kwargs) it was built with, for precedence checks."""

    last: "RecordingSource | None" = None

    def __init__(self, source_type: str, **kwargs) -> None:
        self.name = source_type
        self.source_type = source_type
        self.kwargs = kwargs
        RecordingSource.last = self

    def metadata(self):
        return {"source": self.name, "kind": "recording"}

    def poll(self):
        return [{"voltage_volt": 3.7, "current_ampere": -0.1}]

    def close(self):
        pass


class LeakyMetaSource:
    """A poorly-behaved source that reflects its kwargs (incl. api_key) in metadata()."""

    def __init__(self, source_type: str, **kwargs) -> None:
        self.name = source_type
        self._kwargs = kwargs

    def metadata(self):
        return {"source": self.name, "kind": "leaky", **self._kwargs}

    def poll(self):
        return [{"voltage_volt": 3.7, "current_ampere": -0.1}]

    def close(self):
        pass


class LoggingSecretSource:
    """A source that carelessly logs its own credential every poll."""

    def __init__(self, source_type: str, **kwargs) -> None:
        self.name = source_type
        self._api_key = kwargs.get("api_key")

    def metadata(self):
        return {"source": self.name}

    def poll(self):
        logging.getLogger("battfeed.sources.fake").warning(
            "connecting with api key %s", self._api_key
        )
        return [{"voltage_volt": 3.7, "current_ampere": -0.1}]

    def close(self):
        pass


class RaisingInitSource:
    """A source whose constructor rejects the credential, echoing it in the error."""

    def __init__(self, source_type: str, **kwargs) -> None:
        raise ValueError(f"authentication rejected for api key {kwargs.get('api_key')}")


class LeakyImportSource:
    """A one-shot import source that reflects its api_key in metadata()."""

    def __init__(self, source_type: str, **kwargs) -> None:
        self.name = source_type
        self._kwargs = kwargs
        self._done = False

    def metadata(self):
        return {"source": self.name, "kind": "leaky-import", **self._kwargs}

    def poll(self):
        if self._done:
            return []
        self._done = True
        return [
            {
                "series_id": "packA",
                "run_id": "run1",
                "test_time_second": 0.0,
                "voltage_volt": 3.7,
                "current_ampere": -0.1,
            }
        ]

    def drained(self):
        return self._done

    def close(self):
        pass


def _install(monkeypatch, cls):
    monkeypatch.setattr(cli, "create_source", lambda source_type, **kw: cls(source_type, **kw))


def _short_collect(source, *extra):
    """Args for a fast, bounded collect run of ``source``."""
    return ["collect", "--source", source, "--duration", "0.03", "--interval", "0.01", *extra]


def _sidecar_path(out_path):
    """The .meta.json sidecar written next to a .bdf.csv output file."""
    return out_path.with_name(out_path.name[: -len(".bdf.csv")] + ".meta.json")


def _sidecar(out_path):
    """Load and parse that sidecar."""
    return json.loads(_sidecar_path(out_path).read_text("utf-8"))


# -- precedence: source kwargs (--opt > config > default) --------------------


def test_config_source_block_supplies_kwargs(tmp_path, monkeypatch):
    _install(monkeypatch, RecordingSource)
    cfg = _write_config(
        tmp_path,
        "[source.mc3000]\nslot = 1\ntransport = 'ble'\naddress = 'AA:BB:CC:DD:EE:FF'\n",
    )
    out = tmp_path / "o.bdf.csv"
    assert cli.main(_short_collect("mc3000", "--config", str(cfg), "--out", str(out))) == 0
    assert RecordingSource.last.kwargs == {
        "slot": 1,
        "transport": "ble",
        "address": "AA:BB:CC:DD:EE:FF",
    }


def test_opt_overrides_config_source_block(tmp_path, monkeypatch):
    _install(monkeypatch, RecordingSource)
    cfg = _write_config(tmp_path, "[source.mc3000]\nslot = 1\ntransport = 'ble'\n")
    out = tmp_path / "o.bdf.csv"
    rc = cli.main(
        _short_collect("mc3000", "--config", str(cfg), "--out", str(out), "--opt", "slot=3")
    )
    assert rc == 0
    assert RecordingSource.last.kwargs == {"slot": 3, "transport": "ble"}  # --opt wins


def test_source_type_field_aliases_the_source(tmp_path, monkeypatch):
    _install(monkeypatch, RecordingSource)
    cfg = _write_config(tmp_path, "[source.bay2]\ntype = 'simulator'\nslot = 2\n")
    out = tmp_path / "o.bdf.csv"
    assert cli.main(_short_collect("bay2", "--config", str(cfg), "--out", str(out))) == 0
    assert RecordingSource.last.source_type == "simulator"  # the block's type, not the name
    assert RecordingSource.last.kwargs == {"slot": 2}  # 'type' is consumed, not a kwarg


# -- precedence: run parameters (CLI flag > config > default) ----------------


def test_run_param_comes_from_config_when_no_flag(tmp_path):
    cfg = _write_config(tmp_path, "[collect]\ninterval = 9.0\ninstitution = 'SINTEF'\n")
    out = tmp_path / "o.bdf.csv"
    # No --interval / --institution on the CLI: both resolve from [collect].
    assert (
        cli.main(
            [
                "collect",
                "--source",
                "simulator",
                "--config",
                str(cfg),
                "--out",
                str(out),
                "--duration",
                "0.03",
            ]
        )
        == 0
    )
    meta = _sidecar(out)["metadata"]
    assert meta["requested_interval_second"] == 9.0
    assert meta["institution"] == "SINTEF"


def test_cli_flag_overrides_config_run_param(tmp_path):
    cfg = _write_config(tmp_path, "[collect]\ninterval = 9.0\ninstitution = 'SINTEF'\n")
    out = tmp_path / "o.bdf.csv"
    assert (
        cli.main(
            _short_collect(
                "simulator",
                "--config",
                str(cfg),
                "--out",
                str(out),
                "--interval",
                "0.01",
                "--institution",
                "LAB",
            )
        )
        == 0
    )
    meta = _sidecar(out)["metadata"]
    assert meta["requested_interval_second"] == 0.01  # CLI flag wins
    assert meta["institution"] == "LAB"


# -- credential hygiene: the sidecar on disk ---------------------------------


def test_secret_via_opt_absent_from_sidecar(tmp_path, monkeypatch):
    _install(monkeypatch, LeakyMetaSource)
    out = tmp_path / "o.bdf.csv"
    assert cli.main(_short_collect("leaky", "--out", str(out), "--opt", f"api_key={SECRET}")) == 0
    raw = _sidecar_path(out).read_text("utf-8")
    assert SECRET not in raw
    assert json.loads(raw)["metadata"]["source"]["api_key"] == REDACTED


def test_secret_via_config_absent_from_sidecar(tmp_path, monkeypatch):
    _install(monkeypatch, LeakyMetaSource)
    cfg = _write_config(tmp_path, f"[source.leaky]\napi_key = '{SECRET}'\n")
    out = tmp_path / "o.bdf.csv"
    assert cli.main(_short_collect("leaky", "--config", str(cfg), "--out", str(out))) == 0
    assert SECRET not in _sidecar_path(out).read_text("utf-8")


def test_secret_via_env_expansion_absent_from_sidecar(tmp_path, monkeypatch):
    _install(monkeypatch, LeakyMetaSource)
    monkeypatch.setenv("MY_SECRET", SECRET)
    cfg = _write_config(tmp_path, "[source.leaky]\napi_key = '${ENV:MY_SECRET}'\n")
    out = tmp_path / "o.bdf.csv"
    assert cli.main(_short_collect("leaky", "--config", str(cfg), "--out", str(out))) == 0
    assert SECRET not in _sidecar_path(out).read_text("utf-8")


def test_secret_absent_from_import_sidecars(tmp_path, monkeypatch):
    """The import verb writes through a RoutingSink whose BdfCsvSink children
    apply the same sidecar redaction."""
    _install(monkeypatch, LeakyImportSource)
    out_dir = tmp_path / "imported"
    rc = cli.main(
        [
            "import",
            "--source",
            "leaky",
            "--out-dir",
            str(out_dir),
            "--interval",
            "0.01",
            "--opt",
            f"api_key={SECRET}",
        ]
    )
    assert rc == 0
    sidecars = list(out_dir.glob("*.meta.json"))
    assert sidecars  # a file was produced
    for sidecar in sidecars:
        text = sidecar.read_text("utf-8")
        assert SECRET not in text
        assert json.loads(text)["metadata"]["source"]["api_key"] == REDACTED


# -- credential hygiene: captured logs at --verbose --------------------------


def test_secret_absent_from_verbose_logs(tmp_path, monkeypatch):
    _install(monkeypatch, LoggingSecretSource)
    captured: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = Capture(level=logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    previous_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        out = tmp_path / "o.bdf.csv"
        # --verbose is a top-level flag, so it precedes the subcommand.
        rc = cli.main(
            ["--verbose", *_short_collect("leaky", "--out", str(out), "--opt", f"api_key={SECRET}")]
        )
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)

    assert rc == 0
    blob = "\n".join(captured)
    assert SECRET not in blob  # the careless source's key never reaches a handler
    assert f"api key {REDACTED}" in blob  # ...but the line WAS logged, just masked


# -- credential hygiene: error message from a failed run ---------------------


def test_secret_absent_from_error_message(tmp_path, monkeypatch, capsys):
    _install(monkeypatch, RaisingInitSource)
    out = tmp_path / "o.bdf.csv"
    rc = cli.main(_short_collect("leaky", "--out", str(out), "--opt", f"api_key={SECRET}"))
    assert rc == 2
    err = capsys.readouterr().err
    assert SECRET not in err
    assert REDACTED in err  # the offending key is masked in the message


# -- credential hygiene: the sources listing ---------------------------------


def test_sources_listing_masks_secret_default(monkeypatch, capsys):
    class SecretDefaultSource:
        """Source with a credential baked into its signature default (bad practice)."""

        def __init__(self, host="lab.local", api_key="BAKED-IN-DEFAULT-KEY"):
            self.name = "leaky"

        def metadata(self):
            return {"source": "leaky"}

        def poll(self):
            return []

    monkeypatch.setattr(cli, "available_sources", lambda: {"leaky": SecretDefaultSource})
    assert cli.main(["sources"]) == 0
    out = capsys.readouterr().out
    assert "BAKED-IN-DEFAULT-KEY" not in out
    assert "api_key=***" in out
    assert "host='lab.local'" in out  # non-secret defaults still shown


# -- malformed config through the CLI ----------------------------------------


def test_cli_reports_malformed_config(tmp_path, capsys):
    bad = _write_config(tmp_path, "not = = valid\n")
    rc = cli.main(["collect", "--source", "simulator", "--config", str(bad), "--duration", "0.01"])
    assert rc == 2
    assert "not valid TOML" in capsys.readouterr().err


def test_cli_reports_unset_env_reference(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("DEFINITELY_UNSET_VAR", raising=False)
    cfg = _write_config(tmp_path, "[source.simulator]\napi_key = '${ENV:DEFINITELY_UNSET_VAR}'\n")
    rc = cli.main(["collect", "--source", "simulator", "--config", str(cfg), "--duration", "0.01"])
    assert rc == 2
    assert "DEFINITELY_UNSET_VAR" in capsys.readouterr().err


# ===========================================================================
# Red-team hardening round 2 (rt-wp31)
# ===========================================================================


class KwargLoggingSource:
    """Reflects every kwarg into metadata() AND logs each one -- exercises both
    the sidecar and the log path for an arbitrary credential name."""

    def __init__(self, source_type: str, **kwargs) -> None:
        self.name = source_type
        self._kwargs = kwargs

    def metadata(self):
        return {"source": self.name, **self._kwargs}

    def poll(self):
        for key, value in self._kwargs.items():
            logging.getLogger("battfeed.sources.fake").warning("cfg %s=%s", key, value)
        return [{"voltage_volt": 3.7, "current_ampere": -0.1}]

    def close(self):
        pass


# -- MAJOR-1: broadened credential-name matcher ------------------------------


def test_broadened_name_absent_from_sidecar_and_logs(tmp_path, monkeypatch):
    """'authorization' is invisible to the old key|token|secret|password pattern."""
    _install(monkeypatch, KwargLoggingSource)
    out = tmp_path / "o.bdf.csv"
    with _capture_all_logs() as logs:
        rc = cli.main(
            [
                "--verbose",
                *_short_collect(
                    "leaky", "--out", str(out), "--opt", f"authorization=Bearer {SECRET}"
                ),
            ]
        )
    assert rc == 0
    assert SECRET not in _sidecar_path(out).read_text("utf-8")
    assert _sidecar(out)["metadata"]["source"]["authorization"] == REDACTED
    assert SECRET not in "\n".join(logs)


@pytest.mark.parametrize("name", ["passphrase", "session_cookie", "db_password", "bearer_token"])
def test_more_broadened_names_masked_in_sidecar(tmp_path, monkeypatch, name):
    _install(monkeypatch, KwargLoggingSource)
    out = tmp_path / "o.bdf.csv"
    assert cli.main(_short_collect("leaky", "--out", str(out), "--opt", f"{name}={SECRET}")) == 0
    assert _sidecar(out)["metadata"]["source"][name] == REDACTED


# -- MAJOR-2: value-based scrubbing + URL userinfo ---------------------------


def test_url_userinfo_secret_absent_from_sidecar_and_logs(tmp_path, monkeypatch):
    """A credential embedded in a URL under the non-secret key 'endpoint'."""
    _install(monkeypatch, KwargLoggingSource)
    out = tmp_path / "o.bdf.csv"
    url = f"https://user:{SECRET}@host/ingest"
    with _capture_all_logs() as logs:
        rc = cli.main(
            ["--verbose", *_short_collect("leaky", "--out", str(out), "--opt", f"endpoint={url}")]
        )
    assert rc == 0
    raw = _sidecar_path(out).read_text("utf-8")
    assert SECRET not in raw
    assert _sidecar(out)["metadata"]["source"]["endpoint"] == "https://***@host/ingest"
    assert SECRET not in "\n".join(logs)


def test_duplicate_secret_under_innocuous_key_scrubbed_by_value(tmp_path, monkeypatch):
    _install(monkeypatch, KwargLoggingSource)
    out = tmp_path / "o.bdf.csv"
    rc = cli.main(
        _short_collect(
            "leaky", "--out", str(out), "--opt", f"api_key={SECRET}", "--opt", f"note=see {SECRET}"
        )
    )
    assert rc == 0
    md = _sidecar(out)["metadata"]["source"]
    assert md["api_key"] == REDACTED  # key-name layer
    assert SECRET not in md["note"] and REDACTED in md["note"]  # value layer


# -- MINOR-3: secret inside an exception traceback ---------------------------


class ExcInfoSource:
    """Logs a caught exception via exc_info=True; the message carries the key."""

    def __init__(self, source_type: str, **kwargs) -> None:
        self.name = source_type
        self._api_key = kwargs.get("api_key")

    def metadata(self):
        return {"source": self.name}

    def poll(self):
        try:
            raise RuntimeError(f"upstream auth failed for key {self._api_key}")
        except RuntimeError:
            logging.getLogger("thirdparty.sdk").error("poll failed", exc_info=True)
        return [{"voltage_volt": 3.7, "current_ampere": -0.1}]

    def close(self):
        pass


def test_secret_in_traceback_is_scrubbed(tmp_path, monkeypatch):
    _install(monkeypatch, ExcInfoSource)
    out = tmp_path / "o.bdf.csv"
    with _capture_all_logs() as logs:
        rc = cli.main(
            ["--verbose", *_short_collect("leaky", "--out", str(out), "--opt", f"api_key={SECRET}")]
        )
    assert rc == 0
    blob = "\n".join(logs)
    assert "RuntimeError" in blob  # the traceback WAS rendered/captured
    assert SECRET not in blob  # ...but the key in its message is masked


# -- MINOR-4: handler on a non-root (propagate=False) logger -----------------


def test_secret_at_source_own_handler_is_scrubbed(tmp_path, monkeypatch):
    seen: list[str] = []

    class SubLoggerSource:
        def __init__(self, source_type: str, **kwargs) -> None:
            self.name = source_type
            self._api_key = kwargs.get("api_key")
            lg = logging.getLogger("vendor.telemetry.wp31")
            lg.setLevel(logging.DEBUG)
            lg.handlers = []
            lg.propagate = False  # its own handler is the ONLY sink -> root filter can't help

            class Own(logging.Handler):
                def emit(self, record: logging.LogRecord) -> None:
                    seen.append(record.getMessage())

            lg.addHandler(Own(level=logging.DEBUG))
            self._lg = lg

        def metadata(self):
            return {"source": self.name}

        def poll(self):
            self._lg.warning("auth header = Bearer %s", self._api_key)
            return [{"voltage_volt": 3.7, "current_ampere": -0.1}]

        def close(self):
            pass

    _install(monkeypatch, SubLoggerSource)
    out = tmp_path / "o.bdf.csv"
    try:
        rc = cli.main(_short_collect("leaky", "--out", str(out), "--opt", f"api_key={SECRET}"))
    finally:
        lg = logging.getLogger("vendor.telemetry.wp31")
        lg.handlers = []
        lg.propagate = True
    assert rc == 0
    blob = "\n".join(seen)
    assert blob  # the source's own handler DID receive the record
    assert SECRET not in blob  # ...scrubbed via the filter attached to non-root handlers


# -- MINOR-5: env-fallback secret (source reads its own DJI_API_KEY) ----------


def test_env_fallback_arms_secret_values(monkeypatch):
    monkeypatch.setenv("DJI_API_KEY", SECRET)
    # kwarg unset -> the dji parser reads DJI_API_KEY itself; the CLI must still
    # know the value so its scrubber is armed.
    assert SECRET in cli._resolve_secret_values("dji", {"path": "."})
    # explicit kwarg -> armed via the key-name path.
    assert SECRET in cli._resolve_secret_values("dji", {"path": ".", "api_key": SECRET})
    # a source with no registered env fallback is not armed from the environment.
    assert SECRET not in cli._resolve_secret_values("mc3000", {"slot": 1})


def test_env_fallback_secret_scrubbed_from_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("DJI_API_KEY", SECRET)

    class EnvKeyLoggingSource:
        def __init__(self, source_type: str, **kwargs) -> None:
            self.name = source_type

        def metadata(self):
            return {"source": self.name}

        def poll(self):
            logging.getLogger("battfeed.sources.dji.fake").warning(
                "using key %s", os.environ.get("DJI_API_KEY")
            )
            return [{"voltage_volt": 3.7, "current_ampere": -0.1}]

        def close(self):
            pass

    _install(monkeypatch, EnvKeyLoggingSource)
    out = tmp_path / "o.bdf.csv"
    with _capture_all_logs() as logs:
        rc = cli.main(["--verbose", *_short_collect("dji", "--out", str(out), "--opt", "path=x")])
    assert rc == 0
    blob = "\n".join(logs)
    assert SECRET not in blob
    assert f"using key {REDACTED}" in blob


# -- MINOR-6: run-param type/positivity validation (no traceback) ------------


@pytest.mark.parametrize("bad", ['interval = "fast"', "interval = 0"])
def test_collect_rejects_bad_interval(tmp_path, monkeypatch, capsys, bad):
    _install(monkeypatch, RecordingSource)
    cfg = _write_config(tmp_path, f"[collect]\n{bad}\n")
    out = tmp_path / "o.bdf.csv"
    rc = cli.main(
        [
            "collect",
            "--source",
            "leaky",
            "--config",
            str(cfg),
            "--out",
            str(out),
            "--duration",
            "0.02",
        ]
    )
    assert rc == 2
    assert "interval must be" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ['interval = "fast"', "interval = 0"])
def test_import_rejects_bad_interval(tmp_path, monkeypatch, capsys, bad):
    _install(monkeypatch, LeakyImportSource)
    cfg = _write_config(tmp_path, f"[import]\n{bad}\n")
    rc = cli.main(
        ["import", "--source", "leaky", "--config", str(cfg), "--out-dir", str(tmp_path / "imp")]
    )
    assert rc == 2
    assert "interval must be" in capsys.readouterr().err


def test_collect_rejects_non_numeric_duration(tmp_path, monkeypatch, capsys):
    _install(monkeypatch, RecordingSource)
    cfg = _write_config(tmp_path, '[collect]\nduration = "soon"\n')
    out = tmp_path / "o.bdf.csv"
    rc = cli.main(["collect", "--source", "leaky", "--config", str(cfg), "--out", str(out)])
    assert rc == 2
    assert "duration must be" in capsys.readouterr().err


# -- NIT: --no-watch overrides a config watch = true -------------------------


def test_no_watch_overrides_config_watch_true(tmp_path, monkeypatch):
    _install(monkeypatch, LeakyImportSource)
    cfg = _write_config(tmp_path, "[import]\nwatch = true\n")
    out_dir = tmp_path / "imp"
    # Without --no-watch this would watch forever; --no-watch forces one-shot,
    # so a drained source lets the command return promptly.
    rc = cli.main(
        [
            "import",
            "--source",
            "leaky",
            "--config",
            str(cfg),
            "--no-watch",
            "--out-dir",
            str(out_dir),
            "--interval",
            "0.01",
        ]
    )
    assert rc == 0
