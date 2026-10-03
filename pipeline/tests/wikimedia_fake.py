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
NO_TABLES_PAGE = '<div class="mw-parser-output"><p>No episode table here.</p></div>'
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
        m = re.search(r"# laminary season check: (Q\d+)", query)
        if m:
            values = re.search(r"VALUES \?item \{([^}]*)\}", query)
            wanted = set(re.findall(r"wd:(Q\d+)", values.group(1))) if values else set()
            check = sparql.get(f"season_check:{m.group(1)}", {"results": {"bindings": []}})
            # One row per statement, like the query: fixtures may leave out ``prop``, which
            # then reads as P179 when an ordinal is given and P361 otherwise.
            bindings = [
                b if "prop" in b else {**b, "prop": {"value": "P179" if "ordinal" in b
                                                     else "P361"}}
                for b in check["results"]["bindings"]
                if b["item"]["value"].rsplit("/", 1)[-1] in wanted
            ]
            return {"head": {"vars": ["item", "prop", "ordinal"]},
                    "results": {"bindings": bindings}}
        if "# laminary list-page statements" in query:
            # fetcher 1.5.2: every P179/P361 value of unverified, linked episode-list pages.
            # Fixtures list {"item", "prop", "value"} rows under "list_statements".
            values = re.search(r"VALUES \?item \{([^}]*)\}", query)
            wanted = set(re.findall(r"wd:(Q\d+)", values.group(1))) if values else set()
            rows = sparql.get("list_statements", {"results": {"bindings": []}})
            return {"head": {"vars": ["item", "prop", "value"]}, "results": {"bindings": [
                b for b in rows["results"]["bindings"]
                if b["item"]["value"].rsplit("/", 1)[-1] in wanted]}}
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
        if params["action"] == "query" and params.get("prop") == "redirects":
            # fetcher 1.5.3: titles redirecting to a main article ("redirects:<title>")
            return wiki.get(f"redirects:{params['titles']}",
                            {"batchcomplete": True, "query": {"pages": []}})
        if params["action"] == "query":
            key = f"query:{params['titles']}"
            if key in wiki:
                return wiki[key]
            # several titles (or one unknown): merge the single-title entries, like MediaWiki
            pages: dict[str, Any] = {}
            redirects: list[Any] = []
            for title in params["titles"].split("|"):
                entry = wiki.get(f"query:{title}")
                if entry is None:
                    pages.setdefault(title, {"ns": 0, "title": title, "missing": True})
                    continue
                redirects += [r for r in entry["query"].get("redirects", [])
                              if r["from"] != r["to"]]
                for page in entry["query"]["pages"]:
                    pages[page["title"]] = page
            out: dict[str, Any] = {"batchcomplete": True, "query": {"pages": list(pages.values())}}
            if redirects:
                out["query"]["redirects"] = redirects
            return out
        assert params["action"] == "parse"
        if params.get("prop") == "links":
            return wiki.get(f"links:{params['oldid']}", {"parse": {"links": []}})
        if params.get("prop") == "sections":
            return wiki[f"sections:{params['oldid']}"]
        if "section" not in params:
            # the whole page (fetcher 1.5.0, episode tables); a page a fixture doesn't render
            # has no episode table
            return wiki.get(f"page:{params['oldid']}", {"parse": {"text": NO_TABLES_PAGE}})
        return wiki[f"text:{params['oldid']}:{params['section']}"]
