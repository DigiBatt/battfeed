"""Unit tests for battfeed.config: loading, env expansion, and redaction."""

from __future__ import annotations

import pytest

from battfeed import config
from battfeed.config import (
    REDACTED,
    Config,
    ConfigError,
    is_secret_key,
    load_config,
    mask_url_userinfo,
    redact_mapping,
    redact_text,
    redact_value,
    url_userinfo_passwords,
)


def _write(tmp_path, text: str):
    path = tmp_path / "battfeed.toml"
    path.write_text(text, encoding="utf-8")
    return path


# -- redaction helpers -------------------------------------------------------


# The broadened matcher (MAJOR-1): real-world credential names must mask, while
# innocuous names that merely CONTAIN a short secret token as a substring
# (compatibility->pat, path->pat, spinner->pin, asphalt->salt) must NOT.
@pytest.mark.parametrize(
    "name",
    [
        # required should-mask set
        "authorization",
        "passphrase",
        "bearer_token",
        "api_key",
        "access_token",
        "db_password",
        "private_key",
        "session_cookie",
        # plus the short whole-segment tokens and camelCase
        "auth",
        "pat",
        "pin",
        "otp",
        "salt",
        "sig",
        "cred",
        "accessToken",
        "clientSecret",
        "API_KEY",
        "refresh_token",
        "signature",
    ],
)
def test_is_secret_key_masks(name):
    assert is_secret_key(name) is True


@pytest.mark.parametrize(
    "name",
    [
        # required should-NOT set
        "path",
        "format",
        "institution",
        "interval",
        "cell",
        "transport",
        "compatibility",
        # plus over-mask traps the segment split must resist
        "compatible",
        "spinner",
        "asphalt",
        "authors",
        "passenger",
        "bypass",
        "config",
        "address",
        "slot",
        "duration",
        "out_dir",
        "username",
    ],
)
def test_is_secret_key_does_not_mask(name):
    assert is_secret_key(name) is False


def test_redact_value_masks_only_secret_names():
    assert redact_value("api_key", "hunter2") == REDACTED
    assert redact_value("address", "AA:BB") == "AA:BB"


def test_redact_mapping_is_deep_and_covers_lists():
    raw = {
        "path": "/data",
        "api_key": "SECRET1",
        "source": {"token": "SECRET2", "notes": "ok"},
        "items": [{"password": "SECRET3"}, {"host": "h"}],  # non-secret key, secret nested
    }
    red = redact_mapping(raw)
    assert red == {
        "path": "/data",
        "api_key": REDACTED,
        "source": {"token": REDACTED, "notes": "ok"},
        "items": [{"password": REDACTED}, {"host": "h"}],
    }
    # The input is never mutated.
    assert raw["api_key"] == "SECRET1"
    assert raw["source"]["token"] == "SECRET2"


def test_redact_mapping_masks_a_secret_named_container_wholesale():
    # A secret-named key ('creds') masks its whole value, nested contents and all.
    assert redact_mapping({"creds": [{"user": "u"}]}) == {"creds": REDACTED}


def test_redact_text_replaces_longest_first():
    text = "key=ABCDEF and short=ABC"
    assert redact_text(text, ["ABC", "ABCDEF"]) == f"key={REDACTED} and short={REDACTED}"
    # Empty / falsy secrets are ignored and never blanket-replace.
    assert redact_text("unchanged", ["", None]) == "unchanged"


# The value layer (MAJOR-2): a credential embedded in a URL, or copied under an
# innocuous key, is masked even though its key name is not suspicious.


def test_mask_url_userinfo():
    assert mask_url_userinfo("https://user:s3cr3t@host/x") == "https://***@host/x"
    assert mask_url_userinfo("postgres://u:p@db:5432/n") == "postgres://***@db:5432/n"
    assert mask_url_userinfo("just a user@host email") == "just a user@host email"  # no scheme
    assert mask_url_userinfo("https://host/no-userinfo") == "https://host/no-userinfo"


def test_url_userinfo_passwords_extracts_password():
    assert url_userinfo_passwords("https://user:SECRET@host/x") == ["SECRET"]
    assert url_userinfo_passwords("https://user@host") == []  # no password component
    assert url_userinfo_passwords("no url here") == []


def test_redact_text_masks_url_userinfo_without_named_secret():
    # Even with no secret values supplied, URL userinfo is masked.
    assert redact_text("dsn=postgres://u:PW@db/n") == "dsn=postgres://***@db/n"


def test_redact_mapping_value_layer_scrubs_url_and_duplicate():
    secret = "SUPERSECRET"
    raw = {
        "endpoint": f"https://user:{secret}@host/ingest",  # non-secret key, URL userinfo
        "note": f"token is {secret}",  # non-secret key, duplicated value
        "api_key": secret,  # secret key
    }
    red = redact_mapping(raw, [secret])
    blob = str(red)
    assert secret not in blob
    assert red["api_key"] == REDACTED
    assert red["endpoint"] == "https://***@host/ingest"
    assert red["note"] == f"token is {REDACTED}"


def test_redact_mapping_masks_url_userinfo_even_without_values():
    raw = {"endpoint": "https://user:PW@host/x"}
    assert redact_mapping(raw) == {"endpoint": "https://***@host/x"}


# -- loading + precedence structure -----------------------------------------


def test_load_config_parses_sections(tmp_path):
    path = _write(
        tmp_path,
        """
        [collect]
        interval = 2.5
        institution = "SINTEF"

        [import]
        out_dir = "imported"
        watch = true

        [source.mc3000]
        slot = 1
        transport = "ble"
        address = "AA:BB:CC:DD:EE:FF"

        [source.bay2]
        type = "mc3000"
        slot = 2
        """,
    )
    cfg = load_config(path)
    assert cfg.run_params("collect") == {"interval": 2.5, "institution": "SINTEF"}
    assert cfg.run_params("import") == {"out_dir": "imported", "watch": True}
    assert cfg.source_block("mc3000") == {
        "slot": 1,
        "transport": "ble",
        "address": "AA:BB:CC:DD:EE:FF",
    }
    assert cfg.source_block("bay2") == {"type": "mc3000", "slot": 2}
    assert cfg.source_block("nope") is None


def test_source_block_returns_a_copy(tmp_path):
    path = _write(tmp_path, "[source.dji]\npath = 'p'\n")
    cfg = load_config(path)
    block = cfg.source_block("dji")
    assert block is not None
    block["path"] = "MUTATED"
    assert cfg.source_block("dji") == {"path": "p"}  # original untouched


def test_empty_config_is_valid(tmp_path):
    cfg = load_config(_write(tmp_path, ""))
    assert cfg == Config()


# -- environment expansion ---------------------------------------------------


def test_env_expansion_resolves_on_access(tmp_path, monkeypatch):
    monkeypatch.setenv("DJI_API_KEY", "SK-FROM-ENV")
    monkeypatch.setenv("HOSTNAME", "lab-01")
    path = _write(
        tmp_path,
        """
        [source.dji]
        api_key = "${ENV:DJI_API_KEY}"
        endpoint = "https://${ENV:HOSTNAME}/ingest"
        """,
    )
    cfg = load_config(path)
    block = cfg.source_block("dji")
    assert block == {"api_key": "SK-FROM-ENV", "endpoint": "https://lab-01/ingest"}


def test_env_expansion_missing_var_is_actionable(tmp_path):
    path = _write(tmp_path, '[source.dji]\napi_key = "${ENV:NOT_SET_ANYWHERE}"\n')
    cfg = load_config(path)  # loads fine; the error surfaces when the block is used
    with pytest.raises(ConfigError) as excinfo:
        cfg.source_block("dji")
    msg = str(excinfo.value)
    assert "NOT_SET_ANYWHERE" in msg and "not set" in msg


def test_unset_env_in_unused_block_does_not_break_other_blocks(tmp_path, monkeypatch):
    """A multi-source file must let you run one source without every OTHER
    source's secret being set: expansion is lazy, per consumed section."""
    monkeypatch.delenv("UNUSED_SECRET", raising=False)
    path = _write(
        tmp_path,
        """
        [source.mc3000]
        slot = 1

        [source.dji]
        api_key = "${ENV:UNUSED_SECRET}"
        """,
    )
    cfg = load_config(path)
    assert cfg.source_block("mc3000") == {"slot": 1}  # the used block resolves fine
    with pytest.raises(ConfigError):
        cfg.source_block("dji")  # only the unused one would have complained


# -- malformed-config errors are actionable ---------------------------------


def test_missing_file_is_actionable(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / "does-not-exist.toml")
    assert "cannot read config file" in str(excinfo.value)


def test_invalid_toml_is_actionable(tmp_path):
    path = _write(tmp_path, "this is = = not toml\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "not valid TOML" in str(excinfo.value)


def test_unknown_collect_key_is_reported(tmp_path):
    path = _write(tmp_path, "[collect]\nintervall = 2.0\n")  # typo
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    msg = str(excinfo.value)
    assert "intervall" in msg and "[collect]" in msg


def test_source_block_must_be_a_table(tmp_path):
    path = _write(tmp_path, "source = 3\n")
    with pytest.raises(ConfigError):
        load_config(path)


# -- Python 3.10 / no-TOML-parser fallback ----------------------------------


def test_missing_toml_parser_is_actionable(tmp_path, monkeypatch):
    """On 3.10 without tomli, --config must fail with an upgrade hint, not a
    cryptic AttributeError (the tomllib import is conditional)."""
    monkeypatch.setattr(config, "_toml", None)
    with pytest.raises(ConfigError) as excinfo:
        load_config(_write(tmp_path, "[collect]\ninterval = 1.0\n"))
    msg = str(excinfo.value)
    assert "3.11" in msg and "tomli" in msg
