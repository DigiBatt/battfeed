"""Push collected samples to any HTTP endpoint that accepts NDJSON.

A generic ingest client for community registries, lab servers, and custom
platforms: rows are buffered, then POSTed as newline-delimited JSON
(optionally gzipped) on a time-based cadence. Stdlib only.

Delivery semantics
------------------
The sink guarantees **at-least-once** delivery of every accepted row: a
failed POST keeps the whole buffer for the next flush, rows still unsent
when :meth:`HttpPushSink.close` gives up are spooled to disk as a loadable
``.spool.ndjson`` file, and a buffer that outgrows ``max_buffered_rows``
spills its oldest rows to a spool file instead of dropping them. Servers
deduplicate however they choose (row content, timestamps, an id column of
their own); the sink makes no exactly-once claim.

Serialisation
-------------
Every row is serialised to one RFC 8259-valid JSON object per line:
non-finite floats (NaN, +/-inf -- sensor dropout) become ``null`` and are
counted and warned about, ``bytes`` become base64 strings, ``datetime`` /
``date`` values become ISO 8601 strings, and any other non-JSON type falls
back to ``str()``. The literal ``NaN`` / ``Infinity`` tokens the stdlib
would otherwise emit are rejected by strict parsers, so they never reach
the wire or a spool file.

Unlike :class:`battfeed.BdfCsvSink` -- which strips the reserved routing
keys because they are never BDF columns -- this sink **includes**
``series_id`` / ``run_id`` in the payload: a receiving server needs them
to demultiplex one stream into per-object, per-run storage.
"""

from __future__ import annotations

import base64
import datetime
import gzip
import json
import logging
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..protocols import SampleValue

__all__ = ["HttpPushSink"]

logger = logging.getLogger(__name__)

#: Default seconds slept between the retry attempts of :meth:`HttpPushSink.close`.
_CLOSE_RETRY_DELAYS_S: tuple[float, ...] = (2.0, 4.0)

#: Default bound on buffered rows before the oldest are spilled to a spool file.
_MAX_BUFFERED_ROWS = 100_000


def _json_default(value: object) -> str:
    """Fallback serialiser: bytes -> base64, datetime/date -> ISO 8601, else str()."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, datetime.date):  # covers datetime.datetime too
        return value.isoformat()
    return str(value)


def _serialize_row(row: Mapping[str, SampleValue]) -> tuple[str, int]:
    """Serialise one row to an RFC 8259-valid JSON line.

    Non-finite floats (NaN, +/-inf) become ``null`` -- sensor-dropout
    semantics -- because the literal ``NaN`` / ``Infinity`` tokens the stdlib
    would otherwise emit are rejected by strict JSON parsers (and a spool
    file preserving them would poison every later re-send). Returns the JSON
    line and the number of non-finite values that were nulled.
    """
    clean: dict[str, Any] = {}
    nonfinite = 0
    for key, value in row.items():
        if isinstance(value, float) and not math.isfinite(value):
            clean[key] = None
            nonfinite += 1
        else:
            clean[key] = value
    return json.dumps(clean, allow_nan=False, default=_json_default), nonfinite


def _redact_url(url: str) -> str:
    """Strip userinfo and the query string from a URL for safe logging."""
    parts = urllib.parse.urlsplit(url)
    netloc = parts.hostname or ""
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    if parts.username or parts.password:
        netloc = f"***@{netloc}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _reject_ctl_chars(label: str, value: str) -> None:
    """Fail fast on header values that would be rejected (or worse) on the wire."""
    if "\r" in value or "\n" in value:
        raise ValueError(
            f"{label} contains a carriage return or newline, which HTTP forbids "
            "in header values. A trailing newline picked up from an environment "
            "variable or file is the common accident -- call .strip() on the value."
        )


class HttpPushSink:
    """Buffer samples and POST them as newline-delimited JSON to ``url``.

    Each row becomes one JSON object per line, with keys exactly as the
    sample provides them. The reserved routing keys (``series_id`` /
    ``run_id``) are deliberately **included** -- in deliberate contrast to
    :class:`battfeed.BdfCsvSink`, which must strip them -- because they are
    meaningful to a receiving server for demultiplexing one stream into
    per-object, per-run storage.

    Network trouble never propagates out of :meth:`write`: the harvester's
    ``ErrorPolicy`` governs sources, not sinks -- a sink heals itself. A
    failed or non-2xx POST keeps the whole buffer for the next flush (logged
    at warning with counts), so delivery is at-least-once; servers
    deduplicate however they choose. :meth:`close` retries the final flush
    and spools any remaining rows to disk rather than dropping them.

    Args:
        url: Endpoint accepting ``POST`` bodies of newline-delimited JSON.
            Userinfo and query strings are redacted from every log message.
        token: Optional bearer token, sent as ``Authorization: Bearer <token>``.
            Rejected at construction if it contains ``\\r`` or ``\\n``.
        headers: Optional extra headers, merged last (the caller wins over
            every generated header, including ``Authorization``). Names and
            values are rejected at construction if they contain ``\\r``/``\\n``.
        batch_seconds: Cadence of time-based flushing on the injected clock;
            :meth:`write` triggers a flush once this much time has passed
            since the last attempt.
        timeout: Per-request socket timeout in seconds.
        compress: Gzip the request body (``Content-Encoding: gzip``).
        spool_dir: Directory for the ``.spool.ndjson`` file written when
            :meth:`close` cannot deliver the remaining rows (or when the
            buffer overflows ``max_buffered_rows``). Defaults to the current
            working directory so at-least-once holds unconfigured; if the
            directory is unusable, the current working directory is the
            fallback.
        close_retry_delays: Seconds slept between close-time flush attempts;
            ``close()`` makes ``len(close_retry_delays) + 1`` attempts.
        max_buffered_rows: Upper bound on buffered rows. When exceeded, the
            oldest rows are spilled to a spool file immediately (warned, and
            counted in :attr:`records_spooled`) so memory stays bounded while
            at-least-once is preserved.
        clock: Monotonic clock used to pace flushing; injectable for tests.
        sleep: Sleep function used between close-time retries; injectable.

    Attributes:
        records_sent: Rows acknowledged with a 2xx response so far.
        records_spooled: Rows written to spool files (close-time spooling
            plus buffer-overflow spills).
    """

    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        headers: Mapping[str, str] | None = None,
        batch_seconds: float = 15.0,
        timeout: float = 10.0,
        compress: bool = True,
        spool_dir: str | Path | None = None,
        close_retry_delays: Sequence[float] = _CLOSE_RETRY_DELAYS_S,
        max_buffered_rows: int = _MAX_BUFFERED_ROWS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._url = url
        self._redacted_url = _redact_url(url)
        self._token = token
        self._headers = dict(headers or {})
        if token is not None:
            _reject_ctl_chars("token", token)
        for name, value in self._headers.items():
            _reject_ctl_chars(f"header name {name!r}", name)
            _reject_ctl_chars(f"header {name!r} value", value)
        self._batch_seconds = batch_seconds
        self._timeout = timeout
        self._compress = compress
        self._spool_dir = Path(spool_dir) if spool_dir is not None else Path.cwd()
        self._close_retry_delays = tuple(close_retry_delays)
        self._max_buffered_rows = max_buffered_rows
        self._clock = clock
        self._sleep = sleep
        self._buffer: list[str] = []  # one serialised JSON object per entry
        self._nonfinite_pending = 0  # non-finite values nulled since the last flush warning
        self._last_flush_attempt = clock()
        self._closed = False
        self.records_sent = 0
        self.records_spooled = 0

    @property
    def url(self) -> str:
        return self._url

    def write(self, rows: Iterable[Mapping[str, SampleValue]]) -> None:
        """Buffer a batch of samples; flush when ``batch_seconds`` has passed.

        Never raises on network trouble -- a failed flush keeps the buffer
        and the next cadence tick tries again. A buffer that outgrows
        ``max_buffered_rows`` spills its oldest rows to a spool file.
        """
        if self._closed:
            raise ValueError(f"HttpPushSink for {self._redacted_url} is closed")
        for row in rows:
            line, nonfinite = _serialize_row(row)
            self._buffer.append(line)
            self._nonfinite_pending += nonfinite
        if len(self._buffer) > self._max_buffered_rows:
            self._spill_overflow()
        if self._buffer and self._clock() - self._last_flush_attempt >= self._batch_seconds:
            self.flush()

    def flush(self) -> bool:
        """POST the buffered rows as (optionally gzipped) NDJSON.

        Returns True when the buffer is empty afterwards (nothing to send,
        or the server answered 2xx and the buffer was cleared). Any other
        response, a network error, or a request-building error keeps the
        whole buffer for the next flush and returns False; nothing is raised.
        """
        if not self._buffer:
            return True
        if self._nonfinite_pending:
            logger.warning(
                "Replaced %d non-finite values (NaN/inf) with null in the batch for %s",
                self._nonfinite_pending,
                self._redacted_url,
            )
            self._nonfinite_pending = 0
        self._last_flush_attempt = self._clock()
        body = ("\n".join(self._buffer) + "\n").encode("utf-8")
        request_headers = {"Content-Type": "application/x-ndjson"}
        if self._compress:
            body = gzip.compress(body)
            request_headers["Content-Encoding"] = "gzip"
        if self._token is not None:
            request_headers["Authorization"] = f"Bearer {self._token}"
        request_headers.update(self._headers)  # caller wins
        count = len(self._buffer)
        try:
            request = urllib.request.Request(
                self._url, data=body, headers=request_headers, method="POST"
            )
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
        except ValueError as exc:
            # Belt over the construction-time check: a header/URL that urllib
            # rejects must degrade to a kept-buffer failure, not an exception
            # that would bypass close()'s spooling.
            logger.error(
                "POST to %s rejected before sending (%s); keeping %d buffered records",
                self._redacted_url,
                exc,
                count,
            )
            return False
        except (urllib.error.URLError, OSError) as exc:
            logger.warning(
                "POST to %s failed (%s); keeping %d buffered records",
                self._redacted_url,
                exc,
                count,
            )
            return False
        if 200 <= status < 300:
            self.records_sent += count
            self._buffer.clear()
            logger.debug("POSTed %d records to %s (HTTP %d)", count, self._redacted_url, status)
            return True
        logger.warning(
            "POST to %s returned HTTP %d; keeping %d buffered records",
            self._redacted_url,
            status,
            count,
        )
        return False

    def close(self) -> None:
        """Final flush with retries, then spool whatever remains. Idempotent.

        Makes ``len(close_retry_delays) + 1`` flush attempts (sleeping the
        configured delays between them via the injected ``sleep``); rows
        still undelivered are written to
        ``<spool_dir>/<host>-<utcstamp>.spool.ndjson`` -- one JSON object
        per line, loadable for later re-send -- and counted in
        :attr:`records_spooled`, never dropped. With the default delays and
        timeout this blocks at most ~36 s against a hung server (three
        attempts x 10 s timeout, plus 2 s + 4 s of sleep) and ~6 s against
        one that refuses connections promptly.
        """
        if self._closed:
            return
        self._closed = True
        for attempt, delay in enumerate((*self._close_retry_delays, None)):
            if self.flush():
                return
            if delay is not None:
                logger.warning(
                    "Close-time flush attempt %d to %s failed; retrying in %.0f s",
                    attempt + 1,
                    self._redacted_url,
                    delay,
                )
                self._sleep(delay)
        self._spool()

    def _spill_overflow(self) -> None:
        """Spool the oldest rows so the buffer never exceeds ``max_buffered_rows``."""
        overflow = len(self._buffer) - self._max_buffered_rows
        path = self._spool_lines(self._buffer[:overflow])
        if path is None:
            # Spooling failed everywhere: keeping the rows in memory beats
            # dropping them (at-least-once outranks the memory bound).
            return
        del self._buffer[:overflow]
        self.records_spooled += overflow
        logger.warning(
            "Buffer for %s exceeded max_buffered_rows=%d; spilled %d oldest records to %s",
            self._redacted_url,
            self._max_buffered_rows,
            overflow,
            path,
        )

    def _spool(self) -> None:
        count = len(self._buffer)
        path = self._spool_lines(self._buffer)
        if path is None:
            logger.error(
                "Could not spool %d undelivered records for %s anywhere; keeping them in memory",
                count,
                self._redacted_url,
            )
            return
        self._buffer.clear()
        self.records_spooled += count
        logger.error(
            "Could not deliver %d records to %s; spooled them to %s for later re-send",
            count,
            self._redacted_url,
            path,
        )

    def _spool_lines(self, lines: Sequence[str]) -> Path | None:
        """Write JSON lines to a spool file; fall back to the cwd; never raise.

        Returns the path written, or None when every candidate directory
        failed (each failure is logged at error; callers keep the rows).
        """
        host = urllib.parse.urlsplit(self._url).hostname or "endpoint"
        stamp = datetime.datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for directory in dict.fromkeys((self._spool_dir, Path.cwd())):
            try:
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / f"{host}-{stamp}.spool.ndjson"
                seq = 1
                while path.exists():  # never clobber an earlier spool from the same second
                    path = directory / f"{host}-{stamp}-{seq}.spool.ndjson"
                    seq += 1
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            except OSError as exc:
                logger.error(
                    "Could not spool %d records for %s into %s: %s",
                    len(lines),
                    self._redacted_url,
                    directory,
                    exc,
                )
                continue
            return path
        return None
