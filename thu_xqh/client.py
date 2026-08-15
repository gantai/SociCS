"""A deliberately slow HTTP client.

The archive we are pulling from is a small university library server, so the
whole module is built around doing *one* request at a time on *one* reused
connection, with a pause between each. There is no concurrency knob on
purpose: the fastest safe way to take 1700 files off a host like this is to
not look like a crawler in the first place.
"""

from __future__ import annotations

import email.utils
import hashlib
import http.client
import random
import socket
import ssl
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field
from typing import BinaryIO, Optional

DEFAULT_USER_AGENT = (
    "SociCS-thu-xqh-archiver/1.0 "
    "(sequential archival fetcher; one connection; contact via --user-agent)"
)

# Statuses where trying again later is reasonable. 404 is deliberately absent:
# a missing issue is an answer, not a failure.
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Statuses that mean "you are going too fast", as opposed to "I am broken".
THROTTLE_STATUSES = frozenset({429, 503})


class RateLimiter:
    """Paces requests, and only ever gets more cautious.

    ``slow_down`` raises the floor for the rest of the run. We never lower it
    again -- if the server told us once that we were pushing too hard, we take
    it at its word for the remainder of the session.
    """

    def __init__(self, delay: float, jitter: float = 0.25, max_delay: float = 300.0):
        self.base_delay = max(0.0, float(delay))
        self.delay = self.base_delay
        self.jitter = jitter
        self.max_delay = max_delay
        self.throttle_events = 0
        self._last: Optional[float] = None

    def wait(self) -> None:
        if self._last is not None and self.delay > 0:
            spread = self.delay * random.uniform(-self.jitter, self.jitter)
            remaining = self._last + self.delay + spread - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
        self._last = time.monotonic()

    def slow_down(self, factor: float = 2.0) -> float:
        self.throttle_events += 1
        self.delay = min(self.max_delay, max(self.delay, 0.5) * factor)
        return self.delay

    def sleep(self, seconds: float) -> None:
        """Explicit cool-off (Retry-After, backoff) that also resets pacing."""
        if seconds > 0:
            time.sleep(seconds)
        self._last = time.monotonic()


@dataclass
class Response:
    status: int
    reason: str
    headers: dict
    url: str
    body: bytes = b""
    size: int = 0
    sha256: str = ""
    truncated: bool = False
    """True when we stopped reading early; the connection was dropped."""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    def text(self, fallback: str = "utf-8") -> str:
        charset = fallback
        ctype = self.header("content-type")
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";")[0].strip() or fallback
        for encoding in (charset, "utf-8", "gb18030", "latin-1"):
            try:
                return self.body.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return self.body.decode("latin-1", errors="replace")


class TransportError(Exception):
    """Connection-level failure that survived every retry."""


class BudgetExhausted(TransportError):
    """The ``--max-requests`` ceiling was reached.

    Not a failure: the caller asked us to stop after N requests. It subclasses
    TransportError so existing handlers stay safe, but callers that treat
    per-item errors as recoverable must let this one through -- it means the
    whole run is over, not that one file went wrong.
    """


class PoliteClient:
    """Single-connection, rate-limited HTTP/HTTPS client."""

    def __init__(
        self,
        base_url: str,
        *,
        delay: float = 5.0,
        timeout: float = 45.0,
        user_agent: str = DEFAULT_USER_AGENT,
        max_retries: int = 4,
        max_requests: Optional[int] = None,
        logger=None,
    ):
        parts = urllib.parse.urlsplit(base_url if "//" in base_url else "https://" + base_url)
        self.scheme = parts.scheme or "https"
        self.host = parts.netloc
        if not self.host:
            raise ValueError(f"base_url has no host: {base_url!r}")
        self.timeout = timeout
        self.user_agent = user_agent
        self.max_retries = max_retries
        self.max_requests = max_requests
        self.limiter = RateLimiter(delay)
        self.log = logger or (lambda msg: None)
        self.requests_made = 0
        self._conn: Optional[http.client.HTTPConnection] = None
        self._robots: Optional[urllib.robotparser.RobotFileParser] = None

    # -- connection management -------------------------------------------------

    def _connect(self) -> http.client.HTTPConnection:
        if self._conn is not None:
            return self._conn
        if self.scheme == "https":
            self._conn = http.client.HTTPSConnection(
                self.host, timeout=self.timeout, context=ssl.create_default_context()
            )
        else:
            self._conn = http.client.HTTPConnection(self.host, timeout=self.timeout)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- robots ----------------------------------------------------------------

    def robots_allows(self, path: str) -> bool:
        """Check path against robots.txt. A missing/unreadable file means yes."""
        if self._robots is None:
            self._robots = urllib.robotparser.RobotFileParser()
            try:
                resp = self.request("GET", "/robots.txt", retries=1)
            except TransportError:
                self._robots.parse([])
                return True
            if resp.ok and resp.body:
                self._robots.parse(resp.text().splitlines())
            else:
                self._robots.parse([])
        return self._robots.can_fetch(self.user_agent, path)

    # -- requests --------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Optional[dict] = None,
        sink: Optional[BinaryIO] = None,
        max_bytes: Optional[int] = None,
        retries: Optional[int] = None,
        _redirects_left: int = 5,
    ) -> Response:
        """Perform one request, retrying transport errors and 5xx/429.

        ``sink`` streams the body to a file instead of buffering it.
        ``max_bytes`` stops the read early (used for cheap existence probes);
        the connection is closed afterwards rather than reused.
        """
        attempts = self.max_retries if retries is None else retries
        last_error: Optional[Exception] = None

        for attempt in range(attempts):
            if self.max_requests is not None and self.requests_made >= self.max_requests:
                raise BudgetExhausted(
                    f"request budget reached ({self.max_requests} requests)"
                )
            self.limiter.wait()
            try:
                resp = self._perform(method, path, headers or {}, sink, max_bytes)
            except (http.client.HTTPException, socket.error, ssl.SSLError, OSError) as exc:
                self.close()
                last_error = exc
                backoff = min(60.0, 2.0 ** attempt) + random.uniform(0, 1.0)
                self.log(f"  ! {method} {path}: {exc.__class__.__name__}: {exc}; "
                         f"retry in {backoff:.1f}s ({attempt + 1}/{attempts})")
                self.limiter.sleep(backoff)
                continue

            if resp.status in (301, 302, 303, 307, 308) and _redirects_left > 0:
                location = resp.header("location")
                if location:
                    target = urllib.parse.urljoin(f"{self.scheme}://{self.host}{path}", location)
                    tparts = urllib.parse.urlsplit(target)
                    if tparts.netloc and tparts.netloc != self.host:
                        # Off-host redirect: hand it back rather than silently
                        # following something we were not asked to fetch.
                        return resp
                    new_path = tparts.path + (f"?{tparts.query}" if tparts.query else "")
                    return self.request(
                        method, new_path, headers=headers, sink=sink,
                        max_bytes=max_bytes, retries=attempts,
                        _redirects_left=_redirects_left - 1,
                    )

            if resp.status in RETRY_STATUSES and attempt < attempts - 1:
                if resp.status in THROTTLE_STATUSES:
                    new_delay = self.limiter.slow_down()
                    self.log(f"  ! {resp.status} from server; base delay now {new_delay:.1f}s")
                wait_for = self._retry_after(resp) or min(60.0, 2.0 ** attempt) + random.uniform(0, 1)
                self.log(f"  ! {method} {path}: HTTP {resp.status}; retry in {wait_for:.1f}s "
                         f"({attempt + 1}/{attempts})")
                self.limiter.sleep(wait_for)
                continue

            return resp

        raise TransportError(f"{method} {path} failed after {attempts} attempts: {last_error}")

    def _retry_after(self, resp: Response) -> Optional[float]:
        raw = resp.header("retry-after").strip()
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
        try:
            when = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        return max(0.0, when.timestamp() - time.time())

    def _perform(self, method, path, headers, sink, max_bytes) -> Response:
        conn = self._connect()
        request_headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "keep-alive",
        }
        request_headers.update(headers)

        self.requests_made += 1
        conn.request(method, path, headers=request_headers)
        raw = conn.getresponse()

        header_map = {k.lower(): v for k, v in raw.getheaders()}
        digest = hashlib.sha256()
        size = 0
        chunks: list = []
        truncated = False

        if method == "HEAD":
            raw.read()
        else:
            while True:
                block = raw.read(65536)
                if not block:
                    break
                digest.update(block)
                size += len(block)
                if sink is not None:
                    sink.write(block)
                else:
                    chunks.append(block)
                if max_bytes is not None and size >= max_bytes:
                    truncated = True
                    break

        if truncated or header_map.get("connection", "").lower() == "close":
            self.close()

        return Response(
            status=raw.status,
            reason=raw.reason,
            headers=header_map,
            url=f"{self.scheme}://{self.host}{path}",
            body=b"".join(chunks),
            size=size,
            sha256=digest.hexdigest() if size else "",
            truncated=truncated,
        )
