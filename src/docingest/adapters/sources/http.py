"""Polite HTTP for source crawlers (stdlib only).

Open-access archives ask crawlers for the same things: a minimum interval between
requests, a single connection at a time, an identifying User-Agent, and backing off
when told to (429 / 503 + ``Retry-After``). :class:`PoliteClient` enforces all of it in
one place, so every request an adapter makes (search, download, license lookup) draws
on one shared budget. The network sits behind a tiny :class:`Transport` callable and
the clock is injectable, so unit tests replay recorded responses without sockets or
real sleeping.
"""

from __future__ import annotations

import gzip
import http.client
import io
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from ...domain.errors import DocingestError, SourceUnavailableError

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
# Network-side failures worth another attempt (EOFError / zlib.error: cut gzip stream).
TRANSIENT_ERRORS = (OSError, http.client.HTTPException, EOFError, zlib.error)
CHUNK = 1 << 20
Clock = Callable[[], float]
Sleep = Callable[[float], None]


class Body(Protocol):
    """The readable end of a response: an ``http.client`` response, a gzip stream, bytes."""

    def read(self, size: int = ..., /) -> bytes: ...

    def close(self) -> None: ...


@dataclass
class RawResponse:
    """One transport-level response: any status, lower-cased headers, an open body."""

    status: int
    url: str  # final URL, after redirects
    headers: Mapping[str, str]
    body: Body
    wrapped: Body | None = None  # the network stream, when ``body`` decodes it

    def close(self) -> None:
        self.body.close()
        if self.wrapped is not None:
            self.wrapped.close()


class Transport(Protocol):
    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> RawResponse:
        """One GET. Return every HTTP status as a response; raise ``OSError`` on I/O failure."""
        ...


def _lower(headers: Message | Mapping[str, str] | None) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()} if headers is not None else {}


def _decoded(status: int, url: str, headers: dict[str, str], body: Body) -> RawResponse:
    """Undo ``Content-Encoding: gzip`` while streaming (the length then no longer applies)."""
    if headers.get("content-encoding", "").strip().lower() in {"gzip", "x-gzip"}:
        headers.pop("content-encoding")
        headers.pop("content-length", None)
        stream = cast(BinaryIO, body)  # GzipFile only reads it (typeshed also wants seek)
        return RawResponse(status, url, headers, gzip.GzipFile(fileobj=stream, mode="rb"), body)
    return RawResponse(status, url, headers, body)


class UrllibTransport:
    """``urllib.request``: follows redirects (arXiv ``/e-print/`` -> ``/src/``), no keep-alive.

    Sends ``Accept-Encoding: gzip`` unless the caller set one (urllib would otherwise
    send ``identity``) and decodes it transparently. ``proxies=None`` uses the
    environment / system proxy settings; ``{}`` disables proxies.
    """

    def __init__(self, proxies: Mapping[str, str] | None = None):
        handlers = [] if proxies is None else [urllib.request.ProxyHandler(dict(proxies))]
        self._opener = urllib.request.build_opener(*handlers)

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> RawResponse:
        sent = {"Accept-Encoding": "gzip", **headers}
        request = urllib.request.Request(url, headers=sent, method="GET")
        try:
            resp = self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as e:  # 4xx / 5xx still carry headers and a body
            body = e if e.fp is not None else io.BytesIO()
            return _decoded(e.code, e.geturl() or url, _lower(e.headers), body)
        return _decoded(resp.status, resp.geturl(), _lower(resp.headers), resp)


@dataclass(frozen=True)
class HttpResult:
    url: str  # final URL, after redirects
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)  # lower-cased names
    body: bytes = b""  # empty when the body was streamed to a file
    size: int = 0
    seconds: float = 0.0
    attempts: int = 1


class HttpStatusError(SourceUnavailableError):
    """A final HTTP status the caller did not accept (e.g. 404 for a missing source)."""

    def __init__(self, url: str, status: int, detail: str = ""):
        super().__init__(f"HTTP {status} for {url}" + (f": {detail}" if detail else ""))
        self.url = url
        self.status = status


class RateLimiter:
    """Minimum interval between request *starts*. One instance = one shared budget."""

    def __init__(self, interval_s: float, clock: Clock = time.monotonic, sleep: Sleep = time.sleep):
        self.interval_s = interval_s
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> float:
        """Block until a request may start, mark it started; return the seconds waited."""
        waited = 0.0
        if self._last is not None:
            while (remaining := self._last + self.interval_s - self._clock()) > 0:
                self._sleep(remaining)
                waited += remaining
        self._last = self._clock()
        return waited


def retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    """``Retry-After`` as seconds: delta-seconds or an HTTP-date (RFC 9110 10.2.3)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - (now or datetime.now(UTC))).total_seconds(), 0.0)


def with_query(url: str, params: Mapping[str, object] | None) -> str:
    if not params:
        return url
    query = urllib.parse.urlencode({k: str(v) for k, v in params.items()})
    return f"{url}{'&' if '?' in url else '?'}{query}"


class PoliteClient:
    """Rate-limited, serialized, retrying GETs.

    * ``delay_s`` between the starts of any two requests (retries included);
    * one request in flight at a time (a lock held until its body is fully read);
    * ``retries`` extra attempts on 429 / 5xx / network errors / truncated bodies, with
      exponential backoff (``max(delay_s, 1) * 2**attempt``) or the server's
      ``Retry-After``; a ``Retry-After`` longer than ``max_retry_after_s`` fails fast.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        delay_s: float,
        timeout_s: float,
        retries: int,
        transport: Transport | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = time.sleep,
        max_retry_after_s: float = 300.0,
        max_bytes: int = 1 << 30,
    ):
        self.headers = {"User-Agent": user_agent}
        self.timeout_s = timeout_s
        self.retries = max(retries, 0)
        self.backoff_s = max(delay_s, 1.0)
        self.max_retry_after_s = max_retry_after_s
        self.max_bytes = max_bytes
        self.transport: Transport = transport or UrllibTransport()
        self.limiter = RateLimiter(delay_s, clock, sleep)
        self.requests = 0  # every attempt that reached the transport
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()

    def get(
        self,
        url: str,
        params: Mapping[str, object] | None = None,
        *,
        ok: tuple[int, ...] = (200,),
        accept: str = "*/*",
    ) -> HttpResult:
        """GET into memory (API / OAI responses). ``accept`` is the Accept header."""
        return self._request(with_query(url, params), None, ok, accept)

    def download(
        self, url: str, path: Path, *, ok: tuple[int, ...] = (200,), accept: str = "*/*"
    ) -> HttpResult:
        """GET streamed into ``path`` (overwritten on each attempt)."""
        return self._request(url, path, ok, accept)

    # ------------------------------------------------------------------ internals
    def _request(self, url: str, path: Path | None, ok: tuple[int, ...], accept: str) -> HttpResult:
        headers = {**self.headers, "Accept": accept}  # curl-like; urllib sends none
        attempts = self.retries + 1
        last_error = ""
        with self._lock:  # a single connection at a time
            for attempt in range(attempts):
                self.limiter.wait()
                self.requests += 1
                t0 = self._clock()
                try:
                    resp = self.transport(url, headers, self.timeout_s)
                except TRANSIENT_ERRORS as e:
                    last_error = f"{type(e).__name__}: {e}"
                    delay = self._backoff(attempt)
                else:
                    try:
                        if resp.status in ok:
                            body, size = self._read(resp, path)
                            return HttpResult(
                                url=resp.url,
                                status=resp.status,
                                headers=dict(resp.headers),
                                body=body,
                                size=size,
                                seconds=round(self._clock() - t0, 3),
                                attempts=attempt + 1,
                            )
                        if resp.status not in RETRYABLE_STATUS:
                            raise HttpStatusError(url, resp.status, _snippet(resp.body))
                        last_error = f"HTTP {resp.status}"
                        delay = self._retry_delay(url, resp, attempt)
                    except TRANSIENT_ERRORS as e:  # the body failed mid-read
                        last_error = f"{type(e).__name__}: {e}"
                        delay = self._backoff(attempt)
                    finally:
                        resp.close()
                if attempt + 1 < attempts:
                    log.warning(
                        "%s: %s; retry %d/%d in %.1fs",
                        url,
                        last_error,
                        attempt + 1,
                        self.retries,
                        delay,
                    )
                    self._sleep(delay)
        raise SourceUnavailableError(f"{url}: gave up after {attempts} attempt(s) ({last_error})")

    def _backoff(self, attempt: int) -> float:
        return self.backoff_s * 2**attempt

    def _retry_delay(self, url: str, resp: RawResponse, attempt: int) -> float:
        wait = retry_after_seconds(resp.headers.get("retry-after"))
        if wait is None:
            return self._backoff(attempt)
        if wait > self.max_retry_after_s:
            raise HttpStatusError(url, resp.status, f"server asks to retry after {wait:.0f}s")
        return wait

    def _read(self, resp: RawResponse, path: Path | None) -> tuple[bytes, int]:
        size = 0
        chunks: list[bytes] = []
        out = _local(path.open, "wb") if path is not None else None
        try:
            while chunk := resp.body.read(CHUNK):
                size += len(chunk)
                if size > self.max_bytes:
                    raise SourceUnavailableError(
                        f"{resp.url}: response larger than {self.max_bytes >> 20} MiB"
                    )
                if out is None:
                    chunks.append(chunk)
                else:
                    _local(out.write, chunk)
        finally:
            if out is not None:
                out.close()
        expected = resp.headers.get("content-length", "")
        if expected.isdigit() and int(expected) != size:  # connection dropped mid-body
            raise ConnectionError(f"truncated body: {size} of {expected} bytes")
        return b"".join(chunks), size


class LocalWriteError(DocingestError):
    """The download could not be written locally (disk full, permissions): never retried."""


def _local(fn, *args):
    try:
        return fn(*args)
    except OSError as e:  # an OSError here is not a network error: don't retry it
        raise LocalWriteError(f"cannot write download: {e}") from e


def _snippet(body: Body, limit: int = 300) -> str:
    try:
        raw = body.read(limit)
    except TRANSIENT_ERRORS:
        return ""
    return " ".join(raw.decode("utf-8", "replace").split())[:limit]
