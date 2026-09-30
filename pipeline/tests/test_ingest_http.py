"""HTTP client: host allowlist, User-Agent, retries/backoff, rate limiting, caching, offline."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from laminary_pipeline.ingest.http import (
    ALLOWED_HOSTS,
    CONTACT_EMAIL,
    USER_AGENT,
    HostNotAllowed,
    HttpClient,
    HttpError,
    OfflineCacheMiss,
    Request,
    Response,
)

API = "https://en.wikipedia.org/w/api.php"


class Scripted:
    """Transport returning scripted responses (or raising) in order."""

    def __init__(self, *steps: object) -> None:
        self.steps = list(steps)
        self.requests: list[Request] = []

    def __call__(self, req: Request, timeout: float) -> Response:
        self.requests.append(req)
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        assert isinstance(step, Response)
        return step


def ok(payload: object) -> Response:
    return Response(200, json.dumps(payload).encode(), {})


def client(transport: Scripted, tmp_path: Path | None = None, **kw: object) -> HttpClient:
    sleeps: list[float] = []
    c = HttpClient(transport, tmp_path, sleep=sleeps.append, min_interval={}, **kw)  # type: ignore[arg-type]
    c.sleeps = sleeps  # type: ignore[attr-defined]
    return c


@pytest.mark.parametrize(
    "url",
    [
        "https://api.themoviedb.org/3/movie/603",
        "https://www.themoviedb.org/movie/603",
        "http://en.wikipedia.org/w/api.php",  # not https
        "https://en.wikipedia.org.evil.example/w/api.php",
        "https://fr.wikipedia.org/w/api.php",
    ],
)
def test_refuses_hosts_outside_allowlist(url: str) -> None:
    t = Scripted()
    with pytest.raises(HostNotAllowed):
        client(t).get_json(url)
    assert t.requests == []


def test_allowlist_is_exactly_the_three_wikimedia_hosts() -> None:
    assert ALLOWED_HOSTS == {"en.wikipedia.org", "query.wikidata.org", "www.wikidata.org"}
    assert not any("tmdb" in h or "themoviedb" in h for h in ALLOWED_HOSTS)


def test_sends_descriptive_user_agent_with_contact() -> None:
    t = Scripted(ok({"x": 1}))
    assert client(t).get_json(API, {"action": "query"}) == {"x": 1}
    ua = t.requests[0].headers["User-Agent"]
    assert ua == USER_AGENT and CONTACT_EMAIL in ua and ua.startswith("LaminaryPipeline/")


def test_retries_5xx_and_429_with_backoff_and_retry_after() -> None:
    t = Scripted(
        Response(503, b"busy", {}),
        Response(429, b"slow down", {"retry-after": "7"}),
        urllib.error.URLError("reset"),
        ok({"done": True}),
    )
    c = client(t, backoff_base=1.0)
    assert c.get_json(API) == {"done": True}
    assert len(t.requests) == 4
    assert c.sleeps == [1.0, 7.0, 4.0]  # type: ignore[attr-defined]


def test_retries_mediawiki_maxlag() -> None:
    lag = {"error": {"code": "maxlag", "info": "Waiting for a database server"}}
    t = Scripted(Response(200, json.dumps(lag).encode(), {"retry-after": "5"}), ok({"fine": 1}))
    c = client(t)
    assert c.get_json(API) == {"fine": 1}
    assert c.sleeps == [5.0]  # type: ignore[attr-defined]


def test_gives_up_after_max_retries() -> None:
    t = Scripted(*[Response(502, b"bad gateway", {}) for _ in range(3)])
    with pytest.raises(HttpError) as err:
        client(t, max_retries=2).get_json(API)
    assert err.value.status == 502 and len(t.requests) == 3


def test_does_not_retry_client_errors() -> None:
    t = Scripted(Response(404, b"nope", {}))
    with pytest.raises(HttpError):
        client(t).get_json(API)
    assert len(t.requests) == 1


def test_rate_limit_spaces_requests_per_host() -> None:
    now = [100.0]
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    t = Scripted(ok({}), ok({}))
    c = HttpClient(t, None, sleep=sleep, clock=lambda: now[0],
                   min_interval={"en.wikipedia.org": 0.5})
    c.get_json(API, {"a": 1})
    now[0] += 0.1
    c.get_json(API, {"a": 2})
    assert sleeps == [pytest.approx(0.4)]


def test_cache_forever_ttl_and_offline(tmp_path: Path) -> None:
    clock = [1_000.0]
    t = Scripted(ok({"v": 1}), ok({"v": 2}))
    c = HttpClient(t, tmp_path, sleep=lambda s: None, min_interval={}, wallclock=lambda: clock[0])
    assert c.get_json(API, {"q": 1}, cache_ttl=None) == {"v": 1}
    clock[0] += 10 ** 9
    assert c.get_json(API, {"q": 1}, cache_ttl=None) == {"v": 1}  # forever
    assert c.get_json(API, {"q": 1}, cache_ttl=60) == {"v": 2}  # expired -> refetched
    assert len(t.requests) == 2

    off = HttpClient(Scripted(), tmp_path, offline=True)
    assert off.get_json(API, {"q": 1}, cache_ttl=0) == {"v": 2}  # offline reads any age
    with pytest.raises(OfflineCacheMiss):
        off.get_json(API, {"q": "not cached"})
    assert off.network_requests == 0


def test_post_body_is_part_of_cache_key(tmp_path: Path) -> None:
    t = Scripted(ok({"a": 1}), ok({"b": 2}))
    c = client(t, tmp_path)
    url = "https://query.wikidata.org/sparql"
    assert c.post_form_json(url, {"query": "A"}, cache_ttl=None) == {"a": 1}
    assert c.post_form_json(url, {"query": "B"}, cache_ttl=None) == {"b": 2}
    assert c.post_form_json(url, {"query": "A"}, cache_ttl=None) == {"a": 1}
    assert len(t.requests) == 2
