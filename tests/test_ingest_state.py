"""ImportLedger: content-hash dedupe, quarantine, persistence, atomicity."""

from __future__ import annotations

import hashlib
import json
import logging
import threading

import pytest

from battfeed import ImportLedger


@pytest.fixture
def ledger_path(tmp_path):
    return tmp_path / "state" / "ledger.json"


def make_file(tmp_path, name: str, content: str):
    path = tmp_path / name
    path.write_bytes(content.encode("utf-8"))
    return path


# -- dedupe ------------------------------------------------------------------


def test_fresh_ledger_has_seen_nothing(tmp_path, ledger_path):
    ledger = ImportLedger(ledger_path)
    log = make_file(tmp_path, "a.txt", "flight data")
    assert not ledger.seen(log)
    assert not ledger.is_quarantined(log)
    assert ledger.ingested_count == 0
    assert ledger.quarantined_count == 0


def test_record_then_seen(tmp_path, ledger_path):
    ledger = ImportLedger(ledger_path)
    log = make_file(tmp_path, "a.txt", "flight data")
    ledger.record(log)
    assert ledger.seen(log)
    assert ledger.ingested_count == 1


def test_seen_is_content_based_not_path_based(tmp_path, ledger_path):
    """A re-plugged SD card mounts elsewhere; same bytes must not re-ingest."""
    ledger = ImportLedger(ledger_path)
    original = make_file(tmp_path, "sdcard1/a.txt".replace("/", "_"), "flight data")
    ledger.record(original)

    elsewhere = tmp_path / "sdcard2"
    elsewhere.mkdir()
    copy = elsewhere / "renamed.txt"
    copy.write_bytes(original.read_bytes())
    assert ledger.seen(copy)


def test_different_content_is_not_seen(tmp_path, ledger_path):
    ledger = ImportLedger(ledger_path)
    ledger.record(make_file(tmp_path, "a.txt", "flight one"))
    assert not ledger.seen(make_file(tmp_path, "b.txt", "flight two"))


def test_record_is_idempotent(tmp_path, ledger_path):
    ledger = ImportLedger(ledger_path)
    log = make_file(tmp_path, "a.txt", "flight data")
    ledger.record(log)
    ledger.record(log)
    assert ledger.ingested_count == 1


def test_seen_on_missing_file_raises(tmp_path, ledger_path):
    ledger = ImportLedger(ledger_path)
    with pytest.raises(FileNotFoundError):
        ledger.seen(tmp_path / "vanished.txt")


# -- quarantine --------------------------------------------------------------


def test_quarantine_roundtrip_with_reason(tmp_path, ledger_path):
    ledger = ImportLedger(ledger_path)
    bad = make_file(tmp_path, "FLY001.DAT", "\x00binary")
    ledger.quarantine(bad, "unsupported .DAT flight record")
    assert ledger.is_quarantined(bad)
    assert ledger.quarantine_reason(bad) == "unsupported .DAT flight record"
    assert ledger.quarantined_count == 1
    # Quarantine and dedupe answer different questions.
    assert not ledger.seen(bad)


def test_quarantine_is_content_based(tmp_path, ledger_path):
    ledger = ImportLedger(ledger_path)
    bad = make_file(tmp_path, "FLY001.DAT", "\x00binary")
    ledger.quarantine(bad, "unsupported")
    copy = make_file(tmp_path, "copied-elsewhere.DAT", "\x00binary")
    assert ledger.is_quarantined(copy)
    assert ledger.quarantine_reason(copy) == "unsupported"


def test_quarantine_reason_none_when_not_quarantined(tmp_path, ledger_path):
    ledger = ImportLedger(ledger_path)
    log = make_file(tmp_path, "a.txt", "fine")
    assert ledger.quarantine_reason(log) is None


def test_requarantine_same_reason_is_quiet_changed_reason_warns(tmp_path, ledger_path, caplog):
    ledger = ImportLedger(ledger_path)
    bad = make_file(tmp_path, "x.DAT", "\x00v1")
    with caplog.at_level(logging.WARNING, logger="battfeed.ingest_state"):
        ledger.quarantine(bad, "reason one")
        ledger.quarantine(bad, "reason one")  # same reason: quiet no-op
        quarantine_logs = [r for r in caplog.records if "Quarantined" in r.getMessage()]
        assert len(quarantine_logs) == 1
        ledger.quarantine(bad, "reason two")  # changed reason: rewrite + warn again
    quarantine_logs = [r for r in caplog.records if "Quarantined" in r.getMessage()]
    assert len(quarantine_logs) == 2
    assert ledger.quarantine_reason(bad) == "reason two"


# -- zero-byte files and hashing ---------------------------------------------


def test_zero_byte_files_are_never_content_keyed(tmp_path, ledger_path):
    """Every empty file shares one sha256; content-keying them would make all
    empty files 'seen' and let them inherit each other's quarantine reasons."""
    ledger = ImportLedger(ledger_path)
    empty1 = tmp_path / "empty1.txt"
    empty1.touch()
    empty2 = tmp_path / "empty2.txt"
    empty2.touch()

    ledger.record(empty1)  # declines: never remembered
    assert not ledger.seen(empty1)
    assert not ledger.seen(empty2)
    assert ledger.ingested_count == 0

    ledger.quarantine(empty1, "no rows in file")  # declines too
    assert not ledger.is_quarantined(empty1)
    assert not ledger.is_quarantined(empty2)  # no inherited quarantine reasons
    assert ledger.quarantine_reason(empty2) is None
    assert ledger.quarantined_count == 0


def test_hash_of_returns_digest_and_none_for_empty(tmp_path, ledger_path):
    ledger = ImportLedger(ledger_path)
    full = make_file(tmp_path, "a.txt", "flight data")
    assert ledger.hash_of(full) == hashlib.sha256(b"flight data").hexdigest()
    empty = tmp_path / "empty.txt"
    empty.touch()
    assert ledger.hash_of(empty) is None


def test_precomputed_content_hash_is_used_not_recomputed(tmp_path, ledger_path):
    """content_hash= lets sources hash each file exactly once per poll."""
    ledger = ImportLedger(ledger_path)
    file_a = make_file(tmp_path, "a.txt", "content A")
    file_b = make_file(tmp_path, "b.txt", "content B")
    ledger.record(file_a, content_hash=ledger.hash_of(file_b))
    assert ledger.seen(file_b)  # proves the precomputed hash was the key ...
    assert not ledger.seen(file_a)  # ... not a re-hash of the path passed in


def test_seen_skip_is_logged_at_info(tmp_path, ledger_path, caplog):
    ledger = ImportLedger(ledger_path)
    original = make_file(tmp_path, "droneA.txt", "TEMPLATE-LOG")
    ledger.record(original)
    twin = make_file(tmp_path, "droneB.txt", "TEMPLATE-LOG")  # identical bytes
    with caplog.at_level(logging.INFO, logger="battfeed.ingest_state"):
        assert ledger.seen(twin)
    messages = [r.getMessage() for r in caplog.records]
    assert any("droneB.txt" in m and "already ingested" in m for m in messages)


# -- persistence -------------------------------------------------------------


def test_ledger_survives_process_restart(tmp_path, ledger_path):
    log = make_file(tmp_path, "a.txt", "flight data")
    bad = make_file(tmp_path, "b.DAT", "\x00binary")
    first = ImportLedger(ledger_path)
    first.record(log)
    first.quarantine(bad, "unsupported")

    reborn = ImportLedger(ledger_path)  # a new process re-opens the same file
    assert reborn.seen(log)
    assert reborn.is_quarantined(bad)
    assert reborn.quarantine_reason(bad) == "unsupported"


def test_reset_clears_everything_durably(tmp_path, ledger_path):
    log = make_file(tmp_path, "a.txt", "flight data")
    bad = make_file(tmp_path, "b.DAT", "\x00binary")
    ledger = ImportLedger(ledger_path)
    ledger.record(log)
    ledger.quarantine(bad, "unsupported")

    ledger.reset()
    assert not ledger.seen(log)
    assert not ledger.is_quarantined(bad)

    reborn = ImportLedger(ledger_path)
    assert not reborn.seen(log)
    assert not reborn.is_quarantined(bad)


def test_ledger_file_is_valid_json_with_no_temp_leftovers(tmp_path, ledger_path):
    ledger = ImportLedger(ledger_path)
    ledger.record(make_file(tmp_path, "a.txt", "flight data"))
    data = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert len(data["ingested"]) == 1
    assert not list(ledger_path.parent.glob("*.tmp"))


def test_corrupt_ledger_raises_actionable_error(ledger_path):
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        ImportLedger(ledger_path)


def test_unknown_ledger_version_raises(ledger_path):
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(json.dumps({"version": 99}), encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        ImportLedger(ledger_path)


def test_string_version_is_rejected(ledger_path):
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text('{"version": "1"}', encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        ImportLedger(ledger_path)


@pytest.mark.parametrize(
    "payload",
    [
        "[1, 2, 3]",  # valid JSON, not an object
        '{"version": 1, "ingested": "oops"}',  # containers of the wrong type
        '{"version": 1, "ingested": ["a", "b"]}',
    ],
)
def test_wrong_shape_valid_json_raises_actionable(ledger_path, payload):
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match="delete the file"):
        ImportLedger(ledger_path)


def test_concurrent_writers_do_not_crash_on_shared_tmp_name(tmp_path, ledger_path):
    """Concurrent imports over one ledger are UNSUPPORTED (lost updates,
    documented) -- but they must degrade to lost updates, never to a crash on
    a shared temp-file name or a corrupted ledger."""
    errors: list[Exception] = []

    def worker(tag: str) -> None:
        try:
            ledger = ImportLedger(ledger_path)  # each writer is its own instance
            for i in range(50):
                path = make_file(tmp_path, f"{tag}-{i}.txt", f"content-{tag}-{i}")
                ledger.record(path)
        except Exception as exc:  # noqa: BLE001 -- the assertion is "no exception at all"
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(tag,)) for tag in ("A", "B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    # The file on disk is intact, parseable JSON (updates may be lost; that is
    # the documented single-writer limitation, not corruption).
    data = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert isinstance(data["ingested"], dict)
