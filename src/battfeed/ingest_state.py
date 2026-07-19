"""Durable import state for batch sources: a dedupe ledger and a quarantine.

A folder-watching import source must answer two questions across process
restarts: *have I ingested this file before?* and *is this file known to be
permanently unsupported?* :class:`ImportLedger` answers both from a single
JSON file.

Content hashes, not paths
-------------------------
Both the ingested set and the quarantine are keyed by the **sha256 of the
file's content**, not its path: a re-plugged SD card that mounts under a new
drive letter must not re-ingest, and a corrupt ``.DAT`` copied somewhere else
is remembered, not retried. The path and a timestamp are recorded alongside
each hash purely as human-readable provenance. The one exception is
**zero-byte files**: every empty file shares one hash, so an entry for it
would make all empty files "seen" (or worse, inherit each other's quarantine
reasons). Empty files are therefore never content-keyed -- ``seen()`` and
``is_quarantined()`` always answer ``False`` for them, ``record()`` and
``quarantine()`` decline (at info level), and each empty file is judged on
its own every run (it may simply still be being written).

Commit point: record only AFTER the write
-----------------------------------------
Do NOT call :meth:`record` inside ``poll()``. At that moment the file's rows
exist only in memory; a crash or sink failure before they reach the sink
would lose the file forever while the ledger swears it was imported (silent
at-most-once). Defer :meth:`record` into the source's ``commit_batch()``
hook, which :func:`battfeed.run_import` calls only after ``sink.write``
returned successfully -- the semantics become **at-least-once**: a crash
between poll and commit leaves the file un-recorded and it is re-imported on
the next run, into NEW segment files (the routing sink reserves output paths
atomically, so a re-import never overwrites earlier data).

Quarantine vs. dedupe
---------------------
The two sets answer different questions. ``record()`` marks a file as
successfully ingested; ``quarantine()`` marks it as *permanently*
unsupported, with a reason the operator can read back (no silent data loss --
invariant I2: a skipped file is counted and explains itself). A transient
parse failure belongs in neither: raise from ``poll()`` and let the driver's
:class:`~battfeed.ErrorPolicy` retry (invariant I4).

ONE writer at a time
--------------------
**A ledger file supports a single writer at a time. Running concurrent
imports over one ledger is unsupported.** Every mutation rewrites the whole
file from this instance's in-memory state, so two concurrent writers silently
lose each other's updates (last writer wins) -- and a lost entry is a file
that will be re-imported. The atomic unique-temp-file writes below guarantee
the file is never *corrupted* by concurrent writers or crashes, not that
their updates merge. Cross-process locking is deliberately out of scope for
now.

Ledger location and lifecycle
-----------------------------
The ledger file's location is the **source's choice** -- importers pass a
path (conventionally beside the data being imported). Note carefully:
**deleting output files does not reset the ledger.** The ledger records what
was *ingested*, not what exists downstream, so re-running an import after
deleting its ``.bdf.csv`` output produces nothing until the ledger is
cleared -- with :meth:`reset`, or ``battfeed import --reset-ledger`` on the
CLI. A corrupt or wrong-shaped ledger file raises rather than being silently
treated as empty (an empty ledger would re-ingest everything -- duplicate
data is data loss's quieter sibling); delete the file to start fresh
(``--reset-ledger`` cannot repair it: the source typically opens the ledger
before the reset hook can run).

Every mutation is persisted immediately with an atomic
write-unique-temp-then-replace, so a crash can never leave a half-written
(invalid JSON) ledger behind.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["ImportLedger"]

logger = logging.getLogger(__name__)

_LEDGER_VERSION = 1

#: Chunk size for content hashing; flight logs are tens of MB, not GB.
_HASH_CHUNK_BYTES = 1 << 20


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ImportLedger:
    """JSON-file-backed dedupe ledger and quarantine for batch import sources.

    Typical use -- hash each candidate file exactly once per poll, and defer
    :meth:`record` into ``commit_batch()`` (the commit point; see the module
    docstring)::

        class FolderImporter:
            def __init__(self, directory):
                self.directory = Path(directory)
                self.ledger = ImportLedger(self.directory / ".import-ledger.json")
                self._pending = None  # (path, content_hash) awaiting commit

            def poll(self):
                for path in sorted(self.directory.glob("*.txt")):
                    digest = self.ledger.hash_of(path)
                    if digest is None:
                        continue  # zero-byte: never content-keyed; re-judged next poll
                    if self.ledger.seen(path, content_hash=digest):
                        continue
                    if self.ledger.is_quarantined(path, content_hash=digest):
                        continue
                    try:
                        rows = parse(path)
                    except UnsupportedFormat as exc:
                        self.ledger.quarantine(path, str(exc), content_hash=digest)
                        continue
                    self._pending = (path, digest)
                    return rows
                return []

            def commit_batch(self):
                # Called by run_import AFTER sink.write succeeded.
                if self._pending is not None:
                    path, digest = self._pending
                    self.ledger.record(path, content_hash=digest)
                    self._pending = None

    All queries and mutations are keyed by the file's content hash (see the
    module docstring for why content, not path -- and why zero-byte files are
    exempt). Pass ``content_hash=`` (from :meth:`hash_of`) to avoid re-reading
    the file for every call; without it, each call hashes the file itself.
    The ledger file is created on the first mutation; constructing against a
    missing file is an empty ledger.

    **Concurrency:** one writer at a time; concurrent imports sharing a
    ledger file lose updates (see the module docstring). This class is
    crash-safe, not multi-writer-safe.

    Args:
        path: The ledger file. Its parent directory is created on demand.

    Raises:
        ValueError: if an existing ledger file is not valid JSON, is valid
            JSON of the wrong shape, or has an unrecognised version -- delete
            the file to start fresh (see the module docstring for why
            corruption is not silently ignored).
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._ingested: dict[str, dict[str, Any]] = {}
        self._quarantined: dict[str, dict[str, Any]] = {}
        self._load()

    @property
    def path(self) -> Path:
        """The ledger file this instance persists to."""
        return self._path

    @property
    def ingested_count(self) -> int:
        """Number of distinct file contents recorded as ingested."""
        return len(self._ingested)

    @property
    def quarantined_count(self) -> int:
        """Number of distinct file contents quarantined."""
        return len(self._quarantined)

    def hash_of(self, path: str | Path) -> str | None:
        """The sha256 content hash keying ``path``, or ``None`` for a zero-byte file.

        Hash once per file and pass the result to :meth:`seen` /
        :meth:`record` / :meth:`quarantine` / :meth:`is_quarantined` via
        ``content_hash=`` -- each of those otherwise re-reads the whole file.
        ``None`` means the file is empty and never content-keyed (see the
        module docstring): the ledger will not remember anything about it.
        """
        target = Path(path)
        if target.stat().st_size == 0:
            return None
        return _sha256_of(target)

    def _key(self, path: str | Path, content_hash: str | None) -> str | None:
        return content_hash if content_hash is not None else self.hash_of(path)

    def seen(self, path: str | Path, *, content_hash: str | None = None) -> bool:
        """Whether a file with this exact content has been recorded as ingested.

        Logs at info whenever it answers ``True``, so a skipped file is
        visible in the run's log rather than silently absent (invariant I2).
        Always ``False`` for zero-byte files.
        """
        digest = self._key(path, content_hash)
        if digest is None:
            return False
        entry = self._ingested.get(digest)
        if entry is None:
            return False
        logger.info(
            "Skipping %s: content already ingested (recorded from %s at %s)",
            path,
            entry.get("path"),
            entry.get("recorded_at"),
        )
        return True

    def record(self, path: str | Path, *, content_hash: str | None = None) -> None:
        """Record the file's content as ingested and persist. Idempotent.

        Call this from ``commit_batch()``, after the rows reached the sink --
        never from inside ``poll()`` (see the module docstring's commit-point
        section). Declines (at info level) for zero-byte files.
        """
        target = Path(path)
        digest = self._key(target, content_hash)
        if digest is None:
            logger.info(
                "Not recording zero-byte file %s (empty files are never content-keyed)", target
            )
            return
        self._ingested[digest] = {
            "path": str(target),
            "recorded_at": _utcnow_iso(),
        }
        self._save()

    def quarantine(self, path: str | Path, reason: str, *, content_hash: str | None = None) -> None:
        """Mark the file's content as permanently unsupported and persist.

        ``reason`` is a human-readable explanation surfaced by
        :meth:`quarantine_reason` -- a quarantined file must explain itself
        (invariant I2). Re-quarantining the same content with the same reason
        is a quiet no-op; a *changed* reason rewrites the entry and warns
        again. Declines (at info level) for zero-byte files.
        """
        target = Path(path)
        digest = self._key(target, content_hash)
        if digest is None:
            logger.info(
                "Not quarantining zero-byte file %s (empty files are never content-keyed)", target
            )
            return
        existing = self._quarantined.get(digest)
        if existing is not None and existing.get("reason") == reason:
            return  # already quarantined for this exact reason; stay quiet
        self._quarantined[digest] = {
            "path": str(target),
            "reason": reason,
            "quarantined_at": _utcnow_iso(),
        }
        self._save()
        logger.warning("Quarantined %s: %s", target, reason)

    def is_quarantined(self, path: str | Path, *, content_hash: str | None = None) -> bool:
        """Whether a file with this exact content is quarantined.

        Always ``False`` for zero-byte files (they are judged afresh each
        run). Logs at info when it answers ``True``, so the skip is visible.
        """
        digest = self._key(path, content_hash)
        if digest is None:
            return False
        entry = self._quarantined.get(digest)
        if entry is None:
            return False
        logger.info("Skipping %s: content is quarantined (%s)", path, entry.get("reason"))
        return True

    def quarantine_reason(self, path: str | Path, *, content_hash: str | None = None) -> str | None:
        """The recorded reason for a quarantined file, or ``None`` if not quarantined."""
        digest = self._key(path, content_hash)
        if digest is None:
            return None
        entry = self._quarantined.get(digest)
        return None if entry is None else str(entry.get("reason", ""))

    def reset(self) -> None:
        """Clear the ingested set and the quarantine, and persist the empty state.

        This is what ``battfeed import --reset-ledger`` reaches through a
        source's ``reset_ledger()`` hook. Deleting *output* files never resets
        the ledger; this does. It cannot repair a *corrupt* ledger file --
        loading one raises before any reset hook can run; delete the file as
        the error message directs.
        """
        self._ingested.clear()
        self._quarantined.clear()
        self._save()
        logger.info("Reset import ledger %s", self._path)

    # -- persistence -------------------------------------------------------

    def _shape_error(self, detail: str) -> ValueError:
        return ValueError(
            f"Import ledger {self._path} {detail}. Refusing to treat it as empty "
            "(that would silently re-ingest everything); delete the file to "
            "start fresh."
        )

    def _load(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return  # a missing ledger is an empty ledger
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise self._shape_error(f"is not valid JSON ({exc})") from exc
        if not isinstance(data, dict):
            raise self._shape_error(f"does not contain a JSON object (got {type(data).__name__})")
        version = data.get("version")
        if version != _LEDGER_VERSION:
            raise ValueError(
                f"Import ledger {self._path} has unrecognised version {version!r} "
                f"(this battfeed reads version {_LEDGER_VERSION}). Delete the file "
                "to start fresh, or upgrade battfeed."
            )
        ingested = data.get("ingested", {})
        quarantined = data.get("quarantined", {})
        if not isinstance(ingested, dict) or not isinstance(quarantined, dict):
            raise self._shape_error(
                "has an unexpected shape ('ingested' and 'quarantined' must be JSON objects)"
            )
        self._ingested = dict(ingested)
        self._quarantined = dict(quarantined)

    def _save(self) -> None:
        payload = {
            "version": _LEDGER_VERSION,
            "ingested": self._ingested,
            "quarantined": self._quarantined,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Unique temp file (mkstemp) then atomic os.replace: a fixed temp name
        # would make two processes sharing a ledger CRASH on each other's temp
        # file; a unique name keeps concurrent writers merely unsupported
        # (lost updates -- see the module docstring), never corrupting.
        fd, tmp_name = tempfile.mkstemp(
            prefix=self._path.name + ".", suffix=".tmp", dir=self._path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, indent=2) + "\n")
            self._replace_into_place(tmp_name)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

    def _replace_into_place(self, tmp_name: str) -> None:
        # On Windows, two processes replacing the same target at the same
        # moment can transiently collide (sharing violation) even though each
        # replace is atomic; retry briefly. This keeps the unsupported
        # concurrent case crash-free -- it does NOT make it lose-free.
        for attempt in range(5):
            try:
                os.replace(tmp_name, self._path)
                return
            except PermissionError:  # pragma: no cover -- Windows-timing dependent
                if attempt == 4:
                    raise
                time.sleep(0.001 * (attempt + 1))
