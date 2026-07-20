"""Flight-record parser -- a thin, robust wrapper around the ``dji-log`` CLI.

``dji-log`` (https://github.com/lvauvillier/dji-log-parser) turns one raw DJI
Fly app flight record into a wide CSV (``OSD.* / BATTERY.* / RECOVER.* / ...``).
This module is ported from a proprietary implementation with the maintainer's
authorization; the logic below is kept intact.

Two facts learned in the proof-of-concept and baked in here:

1. ``DJIFlightRecord_*.txt`` / ``FlightRecord_*.txt`` (app logs) are the
   parseable artifact. Aircraft ``.DAT`` logs are a *different*, undocumented
   format that ``dji-log`` does not read; we reject them with a clear error
   rather than pretend.
2. Records at format version >= 13 (Mavic Air 2 era onward) are encrypted and
   need a DJI **keychain API key** to decrypt. Decryption is **not offline** --
   ``dji-log`` makes a NETWORK call to DJI's keychain API at parse time. It
   takes the key via ``-a``; the key comes from configuration or the
   ``DJI_API_KEY`` environment variable so no secret lands in code, and it is
   redacted from every log line.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

__all__ = [
    "ParseError",
    "UnsupportedFormat",
    "build_command",
    "classify",
    "find_binary",
    "parse_flight",
    "resolve_binary",
]

log = logging.getLogger(__name__)

#: Default names to look for on PATH when no binary is configured.
_BIN_NAMES = ("dji-log", "dji-log.exe")


class UnsupportedFormat(Exception):
    """The file is recognized but not parseable (e.g. aircraft .DAT) -- do not retry."""


class ParseError(Exception):
    """Parsing failed (missing key, missing binary, corrupt file) -- may retry."""


def classify(raw: Path) -> str:
    """Return 'txt' (parseable app record), 'dat' (unsupported), or 'unknown'."""
    name = raw.name.lower()
    if name.endswith(".txt"):
        # App flight records; dji-log is the final judge (it validates the header).
        return "txt"
    if name.endswith(".dat"):
        return "dat"
    return "unknown"


def find_binary(configured: str | None = None) -> str | None:
    """Return a runnable ``dji-log`` path, or ``None`` when none can be found.

    Resolution order: an explicit ``configured`` path, then the ``DJI_LOG_BIN``
    environment variable, then ``dji-log`` / ``dji-log.exe`` on ``PATH``. Used
    by both :func:`resolve_binary` (which raises on failure) and the source's
    ``availability()`` classmethod (which only needs a yes/no).
    """
    configured = configured or os.environ.get("DJI_LOG_BIN")
    if configured:
        return configured if Path(configured).is_file() else None
    for name in _BIN_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def resolve_binary(configured: str | None = None) -> str:
    """Return a runnable ``dji-log`` path or raise :class:`ParseError`."""
    found = find_binary(configured)
    if found is not None:
        return found
    configured = configured or os.environ.get("DJI_LOG_BIN")
    if configured:
        raise ParseError(f"dji_log_bin/DJI_LOG_BIN points at a missing file: {configured}")
    raise ParseError(
        "dji-log binary not found. Install it from "
        "https://github.com/lvauvillier/dji-log-parser/releases and either put "
        "it on PATH or set dji_log_bin / DJI_LOG_BIN to its full path."
    )


def build_command(
    binary: str, raw: Path, out_csv: Path, api_key: str | None, emit_kml: bool
) -> list[str]:
    """Build the ``dji-log`` argv for one record.

    SECURITY (argument injection): the input records come from untrusted media
    (an SD card an attacker may have named a file on). The raw path is therefore
    **absolutized** (``Path.resolve()`` -- rooted, never dash-leading) and placed
    **after a ``"--"`` end-of-options token**, so a file literally named e.g.
    ``--csv=PWNED.txt`` or ``-a EVIL`` in a relative watch directory can never be
    parsed by ``dji-log`` as an option in flag position.
    """
    cmd = [binary, "-c", str(out_csv)]
    if api_key:
        cmd += ["-a", api_key]
    if emit_kml:
        cmd += ["-k", str(out_csv.with_suffix(".kml"))]
    cmd += ["--", str(Path(raw).resolve())]
    return cmd


def parse_flight(
    raw: Path,
    out_csv: Path,
    *,
    binary: str | None = None,
    api_key: str | None = None,
    emit_kml: bool = False,
) -> Path:
    """Parse one raw flight record into ``out_csv``; return the CSV path.

    Raises :class:`UnsupportedFormat` for recognized-but-unparseable files and
    :class:`ParseError` for anything retryable.
    """
    kind = classify(raw)
    if kind == "dat":
        raise UnsupportedFormat(
            f"{raw.name}: aircraft .DAT logs are not supported by dji-log "
            "(different, undocumented format). Provide the matching "
            "DJIFlightRecord_*.txt from the DJI Fly app instead."
        )
    if kind == "unknown":
        raise UnsupportedFormat(f"{raw.name}: unrecognized file type")

    bin_path = resolve_binary(binary)
    api_key = api_key or os.environ.get("DJI_API_KEY")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_command(bin_path, raw, out_csv, api_key, emit_kml)
    # Redact the API key when echoing the command.
    shown = [("***" if i and cmd[i - 1] == "-a" else a) for i, a in enumerate(cmd)]
    log.info("parse: %s", " ".join(shown))

    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=creationflags)
    stderr = (proc.stderr or "").strip()
    if api_key:
        # SECURITY (key leak): dji-log can reflect the -a value back in its
        # stderr; redact it BEFORE the stderr is spliced into any ParseError
        # message or log record, so the key never escapes via that path.
        stderr = stderr.replace(api_key, "***")

    if proc.returncode != 0:
        msg = f"dji-log exit {proc.returncode}: {stderr[:600]}"
        if "API Key is required" in stderr:
            msg += (
                " -> This record is encrypted (format version >= 13). Decrypting it "
                "makes a NETWORK call to DJI's keychain API (import of modern logs is "
                "not offline). Set DJI_API_KEY to a DJI keychain API key and re-run."
            )
            raise ParseError(msg)
        if "UnexpectedEof" in stderr or "failed to fill whole buffer" in stderr:
            # Truncated/corrupt record -- no retry will ever fix it.
            raise UnsupportedFormat(f"{raw.name}: truncated or corrupt record ({msg})")
        raise ParseError(msg)

    # Success requires a real, non-empty CSV with more than just the header.
    if not out_csv.exists() or out_csv.stat().st_size == 0:
        raise ParseError(f"dji-log reported success but {out_csv} is missing/empty")
    with out_csv.open("r", encoding="utf-8", errors="replace") as fh:
        rows = sum(1 for _ in fh) - 1
    if rows < 1:
        raise ParseError(f"{out_csv.name} has a header but no data rows")

    log.info("parsed %s -> %s (%d rows)", raw.name, out_csv.name, rows)
    return out_csv
