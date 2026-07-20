"""HttpPushSink tests against a local (127.0.0.1) capturing HTTP server."""

from __future__ import annotations

import datetime
import gzip
import json
import logging
import socket
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from battfeed import HttpPushSink
from battfeed.sinks.http_push import _redact_url

ROWS = [
    {"test_time_second": 0.0, "voltage_volt": 3.71, "current_ampere": -0.002},
    {"test_time_second": 1.0, "voltage_volt": 3.70, "current_ampere": -0.002},
]


class IngestServer:
    """Threaded stdlib HTTP server capturing every request, with injectable failure."""

    def __init__(self, port: int = 0) -> None:
        #: (method, lowercase-keyed headers, raw body) per request received.
        self.requests: list[tuple[str, dict[str, str], bytes]] = []
        #: Status codes to answer with, consumed FIFO; empty -> default_status.
        self.statuses: deque[int] = deque()
        self.default_status = 200
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), self._make_handler())
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/ingest"

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                headers = {key.lower(): value for key, value in self.headers.items()}
                server.requests.append((self.command, headers, body))
                status = server.statuses.popleft() if server.statuses else server.default_status
                self.send_response(status)
                self.end_headers()

            def log_message(self, *args) -> None:  # keep pytest output clean
                pass

        return Handler

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def ingest_server():
    server = IngestServer()
    yield server
    server.stop()


def _decode_ndjson(body: bytes, *, gzipped: bool = True) -> list[dict]:
    if gzipped:
        body = gzip.decompress(body)
    return [json.loads(line) for line in body.decode("utf-8").splitlines() if line]


def test_flush_posts_gzipped_ndjson_with_standard_headers(ingest_server):
    sink = HttpPushSink(ingest_server.url)
    sink.write(ROWS)
    assert sink.flush() is True

    assert len(ingest_server.requests) == 1
    method, headers, body = ingest_server.requests[0]
    assert method == "POST"
    assert headers["content-type"] == "application/x-ndjson"
    assert headers["content-encoding"] == "gzip"
    assert "authorization" not in headers  # no token given
    assert _decode_ndjson(body) == ROWS
    assert sink.records_sent == 2

    # The buffer was cleared: another flush has nothing to send.
    assert sink.flush() is True
    sink.close()
    assert len(ingest_server.requests) == 1


def test_compress_false_sends_plain_ndjson(ingest_server):
    sink = HttpPushSink(ingest_server.url, compress=False)
    sink.write(ROWS)
    assert sink.flush() is True

    _, headers, body = ingest_server.requests[0]
    assert "content-encoding" not in headers
    assert _decode_ndjson(body, gzipped=False) == ROWS


def test_bearer_token_header(ingest_server):
    sink = HttpPushSink(ingest_server.url, token="s3cret")
    sink.write(ROWS[:1])
    assert sink.flush() is True
    _, headers, _ = ingest_server.requests[0]
    assert headers["authorization"] == "Bearer s3cret"


def test_caller_headers_merge_last_and_win(ingest_server):
    sink = HttpPushSink(
        ingest_server.url,
        token="s3cret",
        headers={"Authorization": "ApiKey abc123", "X-Lab-Station": "bay-7"},
    )
    sink.write(ROWS[:1])
    assert sink.flush() is True
    _, headers, _ = ingest_server.requests[0]
    assert headers["authorization"] == "ApiKey abc123"  # caller wins over the token
    assert headers["x-lab-station"] == "bay-7"
    assert headers["content-type"] == "application/x-ndjson"  # generated headers still present


def test_write_flushes_on_batch_cadence_of_injected_clock(ingest_server, fake_clock):
    sink = HttpPushSink(
        ingest_server.url, batch_seconds=15.0, clock=fake_clock, sleep=fake_clock.sleep
    )
    sink.write(ROWS[:1])
    assert ingest_server.requests == []  # cadence not reached yet
    fake_clock.now = 14.9
    sink.write(ROWS[1:])
    assert ingest_server.requests == []  # still inside the batch window
    fake_clock.now = 15.0
    sink.write([{"test_time_second": 2.0, "voltage_volt": 3.69, "current_ampere": -0.002}])
    assert len(ingest_server.requests) == 1  # one POST carrying all buffered rows
    assert len(_decode_ndjson(ingest_server.requests[0][2])) == 3
    assert sink.records_sent == 3


def test_non_2xx_keeps_buffer_then_succeeds_later(ingest_server):
    ingest_server.statuses.append(503)
    sink = HttpPushSink(ingest_server.url)
    sink.write(ROWS)
    assert sink.flush() is False
    assert sink.records_sent == 0

    assert sink.flush() is True  # server healthy again (default 200)
    assert len(ingest_server.requests) == 2
    assert _decode_ndjson(ingest_server.requests[1][2]) == ROWS  # whole buffer re-sent
    assert sink.records_sent == 2


def test_connection_refused_keeps_buffer_then_succeeds_later():
    with socket.socket() as probe:  # reserve a port that is free, then closed -> refused
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    sink = HttpPushSink(f"http://127.0.0.1:{port}/ingest", timeout=1.0)
    sink.write(ROWS)
    assert sink.flush() is False  # connection refused: returns False, never raises
    assert sink.records_sent == 0

    server = IngestServer(port=port)
    try:
        assert sink.flush() is True
        assert _decode_ndjson(server.requests[0][2]) == ROWS
        assert sink.records_sent == 2
    finally:
        server.stop()


def test_close_retries_then_spools_loadable_ndjson(ingest_server, fake_clock, tmp_path, caplog):
    ingest_server.default_status = 500
    rows = [
        {"series_id": "pack-A", "run_id": "flight-7", "test_time_second": 0.0, "voltage_volt": 3.7},
        {"series_id": "pack-A", "run_id": "flight-7", "test_time_second": 1.0, "voltage_volt": 3.6},
    ]
    sink = HttpPushSink(
        ingest_server.url, spool_dir=tmp_path, clock=fake_clock, sleep=fake_clock.sleep
    )
    sink.write(rows)
    with caplog.at_level(logging.ERROR, logger="battfeed.sinks.http_push"):
        sink.close()

    assert len(ingest_server.requests) == 3  # three flush attempts
    assert fake_clock.sleeps == [2.0, 4.0]  # backoff between the attempts

    spooled = list(tmp_path.glob("*.spool.ndjson"))
    assert len(spooled) == 1
    assert spooled[0].name.startswith("127.0.0.1-")
    lines = spooled[0].read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == rows  # loadable for later re-send
    assert sink.records_spooled == 2
    assert sink.records_sent == 0
    assert str(spooled[0]) in caplog.text

    sink.close()  # idempotent: no fourth request, no second spool file
    assert len(ingest_server.requests) == 3
    assert len(list(tmp_path.glob("*.spool.ndjson"))) == 1

    with pytest.raises(ValueError, match="closed"):
        sink.write(rows)


def test_close_delivers_remaining_rows_without_retry_when_healthy(ingest_server, fake_clock):
    sink = HttpPushSink(ingest_server.url, clock=fake_clock, sleep=fake_clock.sleep)
    sink.write(ROWS)
    sink.close()
    assert len(ingest_server.requests) == 1
    assert fake_clock.sleeps == []
    assert sink.records_sent == 2
    assert sink.records_spooled == 0


def test_routing_keys_are_included_in_payload(ingest_server):
    # Deliberate contrast with BdfCsvSink: series_id/run_id are meaningful to
    # a receiving server for demultiplexing, so the payload keeps them.
    sink = HttpPushSink(ingest_server.url)
    sink.write(
        [
            {
                "series_id": "pack-A",
                "run_id": "flight-7",
                "test_time_second": 0.0,
                "voltage_volt": 3.7,
                "current_ampere": -0.5,
            }
        ]
    )
    assert sink.flush() is True
    (payload,) = _decode_ndjson(ingest_server.requests[0][2])
    assert payload["series_id"] == "pack-A"
    assert payload["run_id"] == "flight-7"
    assert payload["voltage_volt"] == 3.7


def _strict_loads(text: str) -> dict:
    """Parse one JSON object, rejecting the non-RFC NaN/Infinity tokens."""

    def _reject(token: str) -> None:
        raise AssertionError(f"non-RFC 8259 token {token!r} in payload")

    return json.loads(text, parse_constant=_reject)


NONFINITE_ROW = {
    "test_time_second": 0.0,
    "voltage_volt": float("nan"),
    "current_ampere": float("inf"),
    "surface_temperature_celsius": float("-inf"),
}


def test_nonfinite_floats_become_null_on_the_wire(ingest_server, caplog):
    sink = HttpPushSink(ingest_server.url)
    sink.write([NONFINITE_ROW])
    with caplog.at_level(logging.WARNING, logger="battfeed.sinks.http_push"):
        assert sink.flush() is True

    body = gzip.decompress(ingest_server.requests[0][2]).decode("utf-8")
    (payload,) = [_strict_loads(line) for line in body.splitlines() if line]
    assert payload["test_time_second"] == 0.0
    assert payload["voltage_volt"] is None
    assert payload["current_ampere"] is None
    assert payload["surface_temperature_celsius"] is None
    assert "3 non-finite" in caplog.text  # one warning per flush, with the count


def test_nonfinite_floats_become_null_in_spool_file(ingest_server, fake_clock, tmp_path):
    ingest_server.default_status = 500
    sink = HttpPushSink(
        ingest_server.url,
        spool_dir=tmp_path,
        close_retry_delays=(),
        clock=fake_clock,
        sleep=fake_clock.sleep,
    )
    sink.write([NONFINITE_ROW])
    sink.close()

    (spooled,) = tmp_path.glob("*.spool.ndjson")
    (payload,) = [
        _strict_loads(line) for line in spooled.read_text(encoding="utf-8").splitlines() if line
    ]
    assert payload["voltage_volt"] is None
    assert payload["current_ampere"] is None
    assert payload["test_time_second"] == 0.0  # finite values untouched


def test_default_serializer_bytes_and_datetime(ingest_server):
    sink = HttpPushSink(ingest_server.url)
    sink.write(
        [
            {
                "blob": b"\x00\x01",
                "at": datetime.datetime(2026, 7, 19, 12, 0, tzinfo=datetime.timezone.utc),
                "day": datetime.date(2026, 7, 19),
            }
        ]
    )
    assert sink.flush() is True
    (payload,) = _decode_ndjson(ingest_server.requests[0][2])
    assert payload["blob"] == "AAE="  # bytes -> base64
    assert payload["at"] == "2026-07-19T12:00:00+00:00"  # datetime -> ISO 8601
    assert payload["day"] == "2026-07-19"  # date -> ISO 8601


def test_spool_falls_back_to_cwd_when_spool_dir_is_a_file(
    ingest_server, fake_clock, tmp_path, monkeypatch, caplog
):
    ingest_server.default_status = 500
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    fallback_cwd = tmp_path / "cwd"
    fallback_cwd.mkdir()
    monkeypatch.chdir(fallback_cwd)

    sink = HttpPushSink(
        ingest_server.url,
        spool_dir=blocker,
        close_retry_delays=(),
        clock=fake_clock,
        sleep=fake_clock.sleep,
    )
    sink.write(ROWS)
    with caplog.at_level(logging.ERROR, logger="battfeed.sinks.http_push"):
        sink.close()

    (spooled,) = fallback_cwd.glob("*.spool.ndjson")  # rows landed in the cwd fallback
    lines = spooled.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == ROWS
    assert sink.records_spooled == 2
    assert "Could not spool" in caplog.text  # the failed spool_dir attempt was logged


def test_spool_total_failure_keeps_buffer_and_never_raises(
    ingest_server, fake_clock, tmp_path, monkeypatch, caplog
):
    ingest_server.default_status = 500
    sink = HttpPushSink(
        ingest_server.url,
        spool_dir=tmp_path,
        close_retry_delays=(),
        clock=fake_clock,
        sleep=fake_clock.sleep,
    )
    sink.write(ROWS)

    def boom(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    with caplog.at_level(logging.ERROR, logger="battfeed.sinks.http_push"):
        sink.close()  # must not raise

    assert sink.records_spooled == 0
    assert len(sink._buffer) == 2  # rows retained in memory, not dropped
    assert "keeping them in memory" in caplog.text


def test_token_or_header_with_newline_rejected_at_construction(ingest_server):
    with pytest.raises(ValueError, match=r"\.strip\(\)"):
        HttpPushSink(ingest_server.url, token="s3cret\n")  # trailing-newline env var accident
    with pytest.raises(ValueError, match=r"\.strip\(\)"):
        HttpPushSink(ingest_server.url, headers={"X-Lab": "bay-7\r"})
    with pytest.raises(ValueError, match=r"\.strip\(\)"):
        HttpPushSink(ingest_server.url, headers={"X-Lab\n": "bay-7"})


def test_injected_bad_header_still_spools_on_close(ingest_server, fake_clock, tmp_path, caplog):
    sink = HttpPushSink(
        ingest_server.url, spool_dir=tmp_path, clock=fake_clock, sleep=fake_clock.sleep
    )
    sink._headers["X-Evil"] = "a\r\nb"  # bypasses construction validation on purpose
    sink.write(ROWS)
    with caplog.at_level(logging.ERROR, logger="battfeed.sinks.http_push"):
        sink.close()  # ValueError from urllib degrades to kept-buffer failure

    assert ingest_server.requests == []  # nothing ever reached the server
    (spooled,) = tmp_path.glob("*.spool.ndjson")
    lines = spooled.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == ROWS  # close() still spooled everything
    assert sink.records_spooled == 2
    assert "rejected before sending" in caplog.text


def test_close_retry_delays_configurable(ingest_server, fake_clock, tmp_path):
    ingest_server.default_status = 500
    sink = HttpPushSink(
        ingest_server.url,
        spool_dir=tmp_path,
        close_retry_delays=(0.001, 0.002, 0.003),
        clock=fake_clock,
        sleep=fake_clock.sleep,
    )
    sink.write(ROWS)
    sink.close()

    assert len(ingest_server.requests) == 4  # len(delays) + 1 attempts
    assert fake_clock.sleeps == [0.001, 0.002, 0.003]
    assert len(list(tmp_path.glob("*.spool.ndjson"))) == 1


def test_max_buffered_rows_spills_oldest_to_spool(ingest_server, fake_clock, tmp_path, caplog):
    sink = HttpPushSink(
        ingest_server.url,
        spool_dir=tmp_path,
        batch_seconds=1e9,  # cadence never fires; only the overflow bound acts
        max_buffered_rows=3,
        clock=fake_clock,
        sleep=fake_clock.sleep,
    )
    rows = [{"test_time_second": float(i), "voltage_volt": 3.7} for i in range(5)]
    with caplog.at_level(logging.WARNING, logger="battfeed.sinks.http_push"):
        sink.write(rows)

    (spooled,) = tmp_path.glob("*.spool.ndjson")
    lines = spooled.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["test_time_second"] for line in lines] == [0.0, 1.0]  # oldest
    assert sink.records_spooled == 2
    assert "max_buffered_rows=3" in caplog.text

    assert sink.flush() is True  # the newest rows are still buffered and deliverable
    sent = _decode_ndjson(ingest_server.requests[0][2])
    assert [row["test_time_second"] for row in sent] == [2.0, 3.0, 4.0]
    assert sink.records_sent == 3


def test_redact_url_strips_userinfo_and_query():
    assert (
        _redact_url("http://user:hunter2@example.invalid:8443/ingest?api_key=topsecret")
        == "http://***@example.invalid:8443/ingest"
    )
    assert _redact_url("http://127.0.0.1:9/ingest") == "http://127.0.0.1:9/ingest"


def test_failed_flush_logs_redacted_url(caplog):
    with socket.socket() as probe:  # reserve a port that is free, then closed -> refused
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    sink = HttpPushSink(f"http://127.0.0.1:{port}/ingest?api_key=topsecret", timeout=1.0)
    sink.write(ROWS)
    with caplog.at_level(logging.WARNING, logger="battfeed.sinks.http_push"):
        assert sink.flush() is False

    assert "topsecret" not in caplog.text
    assert f"http://127.0.0.1:{port}/ingest" in caplog.text
