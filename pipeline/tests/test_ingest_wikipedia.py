"""Plot fetcher against recorded (synthetic) MediaWiki responses."""

from __future__ import annotations

import urllib.parse
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from wikimedia_fake import FakeWikimedia

from laminary_pipeline.annotation import (
    format_checker,
    load_schema,
    require_wikipedia_sources,
)
from laminary_pipeline.ingest.http import HttpClient
from laminary_pipeline.ingest.text import sha256_text, word_count
from laminary_pipeline.ingest.wikipedia import (
    MIN_WORDS,
    PlotFetcher,
    article_url,
    license_for,
    normalize_heading,
    permalink,
    plot_sections,
)

NOW = "2026-09-30T12:00:00Z"


def fetcher(fake: FakeWikimedia) -> PlotFetcher:
    client = HttpClient(fake, None, sleep=lambda s: None, min_interval={})
    return PlotFetcher(client, clock=lambda: NOW)


def cand(qid: str, title: str | None, media_type: str = "movie") -> dict[str, Any]:
    return {"qid": qid, "enwiki_title": title, "media_type": media_type, "title": title}


def source_validator() -> Draft202012Validator:
    schema = load_schema()
    sub = {"$defs": schema["$defs"], "$ref": "#/$defs/source"}
    return Draft202012Validator(sub, format_checker=format_checker())


def test_movie_plot_section_ok_and_source_passes_the_gate() -> None:
    fake = FakeWikimedia()
    rec = fetcher(fake).fetch(cand("Q9000001", "The Lantern Keeper"))
    assert rec["status"] == "ok", rec
    text = rec["text"]
    assert rec["word_count"] == word_count(text) >= MIN_WORDS
    assert rec["section"] == {"heading": "plot", "index": "1"}
    assert "[1]" not in text and "citation needed" not in text
    assert "Main article" not in text and "fictional lighthouse used as a set" not in text
    assert text.startswith("On a windswept island") and "child's account." in text
    src = rec["source"]
    assert src == {
        "kind": "wikipedia_plot",
        "ref": "https://en.wikipedia.org/wiki/The_Lantern_Keeper",
        "revision": "1200000001",
        "retrieved_at": NOW,
        "license": "CC-BY-SA-4.0",
        "word_count": rec["word_count"],
        "content_sha256": sha256_text(text),
    }
    assert require_wikipedia_sources([src]) == [src]
    assert list(source_validator().iter_errors(src)) == []
    assert rec["permalink"] == (
        "https://en.wikipedia.org/w/index.php?title=The_Lantern_Keeper&oldid=1200000001"
    )
    # Pinned to the revision; the transcluded "Plot" (index T-1) is never requested.
    parse_reqs = [dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.url).query))
                  for r in fake.requests if "action=parse" in r.url]
    assert parse_reqs and all(q["oldid"] == "1200000001" for q in parse_reqs)
    assert [q.get("section") for q in parse_reqs] == [None, "1"]


def test_too_short_is_skipped_with_reason_and_no_text() -> None:
    rec = fetcher(FakeWikimedia()).fetch(cand("Q9000002", "Nizhal Veedu"))
    assert rec["status"] == "skipped" and rec["skip_reason"] == "too_short"
    assert rec["word_count"] < MIN_WORDS
    assert "text" not in rec and "source" not in rec
    assert str(rec["word_count"]) in rec["skip_detail"]


def test_series_falls_through_short_premise_and_table_only_overview() -> None:
    fake = FakeWikimedia()
    rec = fetcher(fake).fetch(cand("Q9000003", "Harbor Lights (TV series)", "tv_series"))
    assert rec["status"] == "ok", rec
    assert rec["section"]["heading"] == "season synopses"
    assert "Season 1" not in rec["text"]  # subsection headings stripped
    assert "Nora Quill" in rec["text"] and "last ship out" in rec["text"]
    assert rec["source"]["license"] == "CC-BY-SA-3.0"  # 2021 revision
    # Tried the overview (tables only) before the synopses, never fetched the short premise.
    queries = [dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.url).query))
               for r in fake.requests]
    sections = [q["section"] for q in queries if "section" in q]
    assert sections == ["3", "4"]


def test_qid_mismatch_is_skipped() -> None:
    rec = fetcher(FakeWikimedia()).fetch(cand("Q9000004", "Starfall Academy", "tv_series"))
    assert rec["skip_reason"] == "qid_mismatch" and "Q9999999" in rec["skip_detail"]


def test_title_from_wikidata_and_disambiguation_page() -> None:
    fake = FakeWikimedia()
    rec = fetcher(fake).fetch(cand("Q9000005", None))
    assert "www.wikidata.org" in fake.hosts
    wd = [r for r in fake.requests if r.host == "www.wikidata.org"]
    assert wd and all("maxlag=5" in r.url for r in wd)  # polite to Wikidata's replicas too
    assert rec["skip_reason"] == "disambiguation_page"


def test_no_enwiki_article_and_missing_page() -> None:
    f = fetcher(FakeWikimedia())
    assert f.fetch(cand("Q9000007", None))["skip_reason"] == "no_enwiki_article"
    assert f.fetch(cand("Q9000008", "Gone Page"))["skip_reason"] == "missing_page"


def test_network_failure_becomes_retryable_fetch_error() -> None:
    def broken(req: Any, timeout: float) -> Any:
        raise OSError("blocked by policy")

    client = HttpClient(broken, None, sleep=lambda s: None, min_interval={}, max_retries=1)
    rec = PlotFetcher(client, clock=lambda: NOW).fetch(cand("Q9000001", "The Lantern Keeper"))
    assert rec["status"] == "skipped" and rec["skip_reason"] == "fetch_error"


def test_no_plot_section() -> None:
    sections = [{"toclevel": 1, "line": "Cast", "index": "1"},
                {"toclevel": 1, "line": "Reception", "index": "2"}]
    assert plot_sections(sections, "movie", "X") == []


def test_section_priority_differs_by_type() -> None:
    secs = [{"toclevel": 1, "line": "Premise", "index": "1"},
            {"toclevel": 1, "line": "Episodes", "index": "2"},
            {"toclevel": 1, "line": "<i>Synopsis</i>", "index": "3"},
            {"toclevel": 2, "line": "Plot", "index": "4"}]
    assert [s.heading for s in plot_sections(secs, "tv_series", "X")] == [
        "synopsis", "episodes", "premise"
    ]
    assert [s.heading for s in plot_sections(secs, "movie", "X")] == ["synopsis", "premise"]


@pytest.mark.parametrize(
    ("ts", "lic"),
    [("2023-06-29T00:00:00Z", "CC-BY-SA-4.0"), ("2023-06-28T23:59:59Z", "CC-BY-SA-3.0")],
)
def test_license_by_revision_date(ts: str, lic: str) -> None:
    assert license_for(ts) == lic


def test_urls_and_headings() -> None:
    assert article_url("Spy × Family") == "https://en.wikipedia.org/wiki/Spy_%C3%97_Family"
    assert permalink("A B", 5).endswith("title=A_B&oldid=5")
    assert normalize_heading("<i>Plot</i>:") == "plot"
