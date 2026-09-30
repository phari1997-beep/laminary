"""A small, polite HTTP client for Wikimedia APIs (stdlib only).

- Host allowlist: only ``ALLOWED_HOSTS`` can be contacted. Anything else, TMDB included,
  raises ``HostNotAllowed`` before a request is built (docs/NARRATIVE_SCHEMA.md section 1).
- Descriptive User-Agent with a contact address, per the Wikimedia User-Agent policy.
- Per-host minimum interval between requests (serial requests only).
- Retries with exponential backoff on 429, 5xx, network errors and MediaWiki ``maxlag``,
  honoring ``Retry-After``.
- On-disk response cache (``data/cache/http``), keyed by method, URL and body. Pinned
  revisions are immutable, so callers cache those forever; everything else gets a TTL.
- ``offline=True`` serves only from the cache and never touches the network (used by tests
  and by ``--offline`` runs).

The transport is injectable so tests run against recorded fixtures with no network.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from laminary_pipeline import __version__

CONTACT_EMAIL = "laminary.hari@gmail.com"
PROJECT_URL = "https://laminary.lovable.app"
USER_AGENT = (
    f"LaminaryPipeline/{__version__} ({PROJECT_URL}; {CONTACT_EMAIL}) "
    f"python-urllib/{sys.version_info.major}.{sys.version_info.minor}"
)

WIKIPEDIA_HOST = "en.wikipedia.org"
WDQS_HOST = "query.wikidata.org"
WIKIDATA_HOST = "www.wikidata.org"
# The only hosts this pipeline may contact. No TMDB host is, or may be, listed here.
ALLOWED_HOSTS = frozenset({WIKIPEDIA_HOST, WDQS_HOST, WIKIDATA_HOST})

# Seconds between requests to the same host. WDQS queries are heavy, so they're spaced out.
DEFAULT_MIN_INTERVAL = {WIKIPEDIA_HOST: 0.2, WIKIDATA_HOST: 0.2, WDQS_HOST: 2.0}
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_BACKOFF_SECONDS = 120.0


class HostNotAllowed(RuntimeError):
    pass


class OfflineCacheMiss(RuntimeError):
    pass


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, detail: str = "") -> None:
        super().__init__(f"HTTP {status} for {url} {detail}".strip())
        self.status = status
        self.url = url


@dataclass(frozen=True)
class Request:
    method: str
    url: str
    data: bytes | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.url).hostname or ""


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)  # lower-cased names


Transport = Callable[[Request, float], Response]


def urllib_transport(req: Request, timeout: float) -> Response:
    """Real network transport. Uses the environment's proxy and CA settings."""
    r = urllib.request.Request(req.url, data=req.data, method=req.method, headers=dict(req.headers))
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:  # noqa: S310 (host allowlisted)
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return Response(resp.status, resp.read(), headers)
    except urllib.error.HTTPError as e:
        headers = {k.lower(): v for k, v in (e.headers or {}).items()}
        return Response(e.code, e.read() or b"", headers)


def check_host(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or host not in ALLOWED_HOSTS:
        raise HostNotAllowed(f"refusing to contact {url!r}: host not in {sorted(ALLOWED_HOSTS)}")
    return host


def build_url(base: str, params: Mapping[str, Any] | None = None) -> str:
    if not params:
        return base
    return f"{base}?{urllib.parse.urlencode(sorted(params.items()))}"


class HttpClient:
    def __init__(
        self,
        transport: Transport | None = None,
        cache_dir: Path | None = None,
        *,
        offline: bool = False,
        min_interval: Mapping[str, float] | None = None,
        max_retries: int = 5,
        backoff_base: float = 1.0,
        timeout: float = 90.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        wallclock: Callable[[], float] = time.time,
    ) -> None:
        self.transport = transport or urllib_transport
        self.cache_dir = cache_dir
        self.offline = offline
        self.min_interval = dict(DEFAULT_MIN_INTERVAL if min_interval is None else min_interval)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.timeout = timeout
        self.sleep = sleep
        self.clock = clock
        self.wallclock = wallclock
        self._last: dict[str, float] = {}
        self.network_requests = 0
        self.cache_hits = 0

    # ---------- public ----------

    def get_json(
        self, url: str, params: Mapping[str, Any] | None = None, *, cache_ttl: float | None = 0
    ) -> Any:
        """GET and parse JSON. ``cache_ttl``: None caches forever, 0 never reads the cache
        (but still writes it), N seconds reads entries younger than N."""
        return self._json(Request("GET", build_url(url, params)), cache_ttl)

    def post_form_json(
        self,
        url: str,
        form: Mapping[str, str],
        *,
        headers: Mapping[str, str] | None = None,
        cache_ttl: float | None = 0,
    ) -> Any:
        body = urllib.parse.urlencode(sorted(form.items())).encode()
        h = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
        return self._json(Request("POST", url, body, h), cache_ttl)

    # ---------- internals ----------

    def _json(self, req: Request, cache_ttl: float | None) -> Any:
        check_host(req.url)
        key = self._cache_key(req)
        cached = self._cache_read(key, cache_ttl)
        if cached is not None:
            self.cache_hits += 1
            return json.loads(cached)
        if self.offline:
            raise OfflineCacheMiss(f"offline and not cached: {req.method} {req.url}")
        text = self._fetch_text(req)
        self._cache_write(key, req, text)
        return json.loads(text)

    def _fetch_text(self, req: Request) -> str:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **req.headers}
        req = Request(req.method, req.url, req.data, headers)
        attempt = 0
        while True:
            self._throttle(req.host)
            wait: float | None = None
            try:
                self.network_requests += 1
                resp = self.transport(req, self.timeout)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                if attempt >= self.max_retries:
                    raise HttpError(0, req.url, f"network error: {e}") from e
            else:
                if resp.status == 200:
                    text = resp.body.decode("utf-8")
                    if not _is_maxlag(text):
                        return text
                    wait = _retry_after(resp.headers)
                elif resp.status not in RETRY_STATUSES or attempt >= self.max_retries:
                    snippet = resp.body[:200].decode("utf-8", "replace")
                    raise HttpError(resp.status, req.url, snippet)
                else:
                    wait = _retry_after(resp.headers)
                if attempt >= self.max_retries:
                    raise HttpError(resp.status, req.url, "maxlag: gave up")
            backoff = min(self.backoff_base * (2**attempt), MAX_BACKOFF_SECONDS)
            self.sleep(min(max(wait or 0.0, backoff), MAX_BACKOFF_SECONDS))
            attempt += 1

    def _throttle(self, host: str) -> None:
        interval = self.min_interval.get(host, 0.0)
        last = self._last.get(host)
        now = self.clock()
        if last is not None and now - last < interval:
            self.sleep(interval - (now - last))
        self._last[host] = self.clock()

    @staticmethod
    def _cache_key(req: Request) -> str:
        h = hashlib.sha256()
        h.update(req.method.encode())
        h.update(b"\n")
        h.update(req.url.encode())
        h.update(b"\n")
        h.update(req.data or b"")
        return h.hexdigest()

    def _cache_path(self, key: str) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / key[:2] / f"{key}.json"

    def _cache_read(self, key: str, ttl: float | None) -> str | None:
        path = self._cache_path(key)
        if path is None or not path.exists():
            return None
        if ttl == 0 and not self.offline:
            return None
        entry = json.loads(path.read_text(encoding="utf-8"))
        if ttl is not None and ttl > 0 and not self.offline:
            if self.wallclock() - entry["fetched_at"] > ttl:
                return None
        return entry["body"]

    def _cache_write(self, key: str, req: Request, text: str) -> None:
        path = self._cache_path(key)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"method": req.method, "url": req.url, "fetched_at": self.wallclock(), "body": text}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


def _retry_after(headers: Mapping[str, str]) -> float | None:
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _is_maxlag(text: str) -> bool:
    if '"maxlag"' not in text:
        return False
    try:
        data = json.loads(text)
    except ValueError:
        return False
    return isinstance(data, dict) and (data.get("error") or {}).get("code") == "maxlag"
