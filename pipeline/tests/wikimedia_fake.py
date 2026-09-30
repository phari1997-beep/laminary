"""A fake Wikimedia transport that serves the recorded fixture responses (no network).

Routes by host: SPARQL POSTs by the ``# laminary ... query`` comment in the query text,
MediaWiki GETs by action/titles/oldid/section, Wikidata ``wbgetentities`` by id. Every request is
recorded so tests can assert which hosts were contacted.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path
from typing import Any

from laminary_pipeline.ingest.http import Request, Response

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ingest" / "wikimedia_responses.json"
EMPTY_POOL = {"head": {"vars": ["item", "sitelinks", "year"]}, "results": {"bindings": []}}


class FakeWikimedia:
    def __init__(self, fixture: Path = FIXTURE) -> None:
        self.data: dict[str, Any] = json.loads(fixture.read_text(encoding="utf-8"))
        self.requests: list[Request] = []

    @property
    def hosts(self) -> set[str]:
        return {r.host for r in self.requests}

    def __call__(self, req: Request, timeout: float) -> Response:
        self.requests.append(req)
        if req.host == "query.wikidata.org":
            payload = self._sparql(req)
        elif req.host == "en.wikipedia.org":
            payload = self._wikipedia(req)
        elif req.host == "www.wikidata.org":
            params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.url).query))
            payload = self.data["wikidata"][f"wbgetentities:{params['ids']}"]
        else:  # pragma: no cover - the client's allowlist stops this first
            raise AssertionError(f"unexpected host {req.host}")
        return Response(200, json.dumps(payload).encode(), {"content-type": "application/json"})

    def _sparql(self, req: Request) -> dict[str, Any]:
        form = dict(urllib.parse.parse_qsl((req.data or b"").decode()))
        query = form["query"]
        sparql = self.data["sparql"]
        m = re.search(r"# laminary pool query: (\S+)", query)
        if m:
            return sparql.get(f"pool:{m.group(1)}", EMPTY_POOL)
        if "# laminary gold-seed lookup" in query:
            return sparql["seed"]
        if "# laminary detail query" in query:
            values = re.search(r"VALUES \?item \{([^}]*)\}", query)
            wanted = set(re.findall(r"wd:(Q\d+)", values.group(1))) if values else set()
            detail = sparql["detail"]
            bindings = [b for b in detail["results"]["bindings"]
                        if b["item"]["value"].rsplit("/", 1)[-1] in wanted]
            return {"head": detail["head"], "results": {"bindings": bindings}}
        raise AssertionError("unrecognized SPARQL query")

    def _wikipedia(self, req: Request) -> dict[str, Any]:
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.url).query))
        assert params.get("format") == "json" and params.get("formatversion") == "2"
        wiki = self.data["wikipedia"]
        if params["action"] == "query":
            key = f"query:{params['titles']}"
            if key not in wiki:
                return {"batchcomplete": True,
                        "query": {"pages": [{"ns": 0, "title": params["titles"], "missing": True}]}}
            return wiki[key]
        assert params["action"] == "parse"
        if params.get("prop") == "sections":
            return wiki[f"sections:{params['oldid']}"]
        return wiki[f"text:{params['oldid']}:{params['section']}"]
