"""CID: years as seasons (run rule ``cid_years``, fetcher 1.5.5, DECISIONS 2026-10-02).

CID's verified episode-list pages have no season headings: each year is an h2 over its episode
table. The rule treats each year heading as one "season" in year order across the split pages,
with the same episode-table caps; sources carry ``year`` and the coverage line states years
("Summary covers 1998–1999 of 1998–2025.", prompt annotate-1.3.0).

Fixtures: real, trimmed English Wikipedia HTML of "List of CID episodes: 1998–2009" and
"List of CID episodes: 2024–present" (``fixtures/ingest/episodes``, licence and permalink in
``sources.json``); the "2010–2014" page and the Wikidata answers are synthetic.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import pytest
from test_ingest_episodes import ep_rows, fixture, heading, page_requests, table
from test_ingest_seasons import NOW, page, sections, words
from wikimedia_fake import FakeWikimedia

from laminary_pipeline.annotate.config import DEFAULT_PROMPT_VERSION
from laminary_pipeline.annotate.inputs import GateError, InputFormatError, gate, parse_plot
from laminary_pipeline.annotate.prompt import (
    build_request,
    format_years,
    labeler_coverage,
    labeler_text,
    load_prompt,
    verify_request,
)
from laminary_pipeline.annotation import validate_record
from laminary_pipeline.gold.template import manifest_rows, template_rows
from laminary_pipeline.ingest.episodes import list_page_seasons, parse_tables, season_episodes
from laminary_pipeline.ingest.http import HttpClient
from laminary_pipeline.ingest.paths import DataPaths, write_json_atomic
from laminary_pipeline.ingest.plots import needs_fetch
from laminary_pipeline.ingest.runs import run_rule_for
from laminary_pipeline.ingest.wikipedia import FETCHER_VERSION, PlotFetcher

CID = "Q252118"
MAIN = "CID (Indian TV series)"
MAIN_REV = 252000
TAIL = "; the summary stops before that year ends)."
LISTS = {  # title -> (pageid, revid, item)
    "List of CID episodes: 2024–present": (2524, 1337101470, "Q132168366"),
    "List of CID episodes: 2010–2014": (2521, 1358902654, "Q126936006"),
    "List of CID episodes: 1998–2009": (2520, 1374865182, "Q126895358"),
}


def year_page(years: dict[int, tuple[int, int]]) -> str:
    """A synthetic year-headed list page: {year: (episodes, words per summary)}."""
    body = ['<div class="mw-content-ltr mw-parser-output">']
    for y, (n, size) in years.items():
        body += [heading(2, str(y)), table(ep_rows(y, n, size))]
    return "".join(body + ["</div>"])


def cid_fake(*, middle: dict[int, tuple[int, int]] | None = None) -> FakeWikimedia:
    """CID with a thin main article linking its three list pages newest first; no Wikidata
    statements (each list page is verified by the main-article link and links back)."""
    fake = FakeWikimedia()
    w = fake.data["wikipedia"]
    w[f"query:{MAIN}"] = page(MAIN, 2525, MAIN_REV, CID)
    w[f"sections:{MAIN_REV}"] = sections(MAIN, ["Premise", "Cast"])
    w[f"text:{MAIN_REV}:1"] = {"parse": {"text": f"<div><p>{words(40, 'premise')}</p></div>"}}
    w[f"links:{MAIN_REV}"] = {"parse": {"links": [
        {"ns": 0, "title": t, "exists": True} for t in LISTS]}}
    html = {"List of CID episodes: 1998–2009": fixture("cid_list_1998_2009"),
            "List of CID episodes: 2010–2014": year_page(middle or {2010: (2, 30),
                                                                     2011: (2, 30)}),
            "List of CID episodes: 2024–present": fixture("cid_list_2024_present")}
    for title, (pid, rev, item) in LISTS.items():
        w[f"query:{title}"] = page(title, pid, rev, item)
        w[f"sections:{rev}"] = sections(title, ["Series overview"])
        w[f"text:{rev}:1"] = {"parse": {"text": "<div><table><tr><td>1</td></tr></table></div>"}}
        w[f"page:{rev}"] = {"parse": {"text": html[title]}}
        w[f"links:{rev}"] = {"parse": {"links": [{"ns": 0, "title": MAIN, "exists": True}]}}
    return fake


def fetch(fake: FakeWikimedia, **kw: Any) -> dict[str, Any]:
    client = HttpClient(fake, None, sleep=lambda s: None, min_interval={})
    cand = {"qid": CID, "enwiki_title": MAIN, "media_type": "tv_series", "title": "CID",
            "year": 1998, "tmdb_id": 1995, "series_status": "unknown",
            "series_status_basis": "none", "number_of_seasons": 2}
    return PlotFetcher(client, clock=lambda: NOW, **kw).fetch(cand)


def _request(rec: dict[str, Any], version: str = DEFAULT_PROMPT_VERSION) -> tuple[Any, Any]:
    return build_request(parse_plot(rec, "t"), model="claude-opus-5-5",
                         prompt=load_prompt(version), effort="medium", max_tokens=16000)


def _header(params: dict[str, Any]) -> str:
    return params["messages"][0]["content"][0]["text"]


@pytest.fixture
def cid_plot() -> dict[str, Any]:
    rec = fetch(cid_fake())
    assert rec["status"] == "ok", rec.get("skip_detail")
    return rec


@pytest.fixture
def partial_plot() -> dict[str, Any]:
    """The cap falls inside 2010: 1998 and 1999 whole, 2010 in part."""
    full = fetch(cid_fake(middle={2010: (4, 60), 2011: (2, 60)}))
    first_two = sum(p["source"]["word_count"] for p in full["sources"][:2])
    one_episode = len(full["sources"][2]["text"].split("\n\n")[0].split())
    rec = fetch(cid_fake(middle={2010: (4, 60), 2011: (2, 60)}),
                season_word_cap=first_two + one_episode + 5)
    assert rec["status"] == "ok"
    return rec


# --- the rule and the real markup -----------------------------------------------------------


def test_cid_has_the_year_rule() -> None:
    rule = run_rule_for(CID)
    assert rule is not None and rule.name == "cid_years" and rule.numbering == "year"


def test_real_cid_page_year_headings_are_seasons_numbered_by_year() -> None:
    rule = run_rule_for(CID)
    tables = parse_tables(fixture("cid_list_1998_2009"))
    assert [t.heading for t in tables] == ["1998", "1999"]
    years, skipped = list_page_seasons(tables, rule)
    assert [y for y, _ in years] == [1998, 1999] and skipped == []
    # without the rule the same tables are "not under a season heading" (the 1.5.2 result)
    assert [s["reason"] for s in list_page_seasons(tables)[1]] == [
        "not under a season heading"] * 2
    eps = season_episodes(1998, years[0][1], "year").episodes
    assert [e.marker for e in eps] == ["1998E1", "1998E2", "1998E3"]
    assert eps[0].paragraph == ('1998E1 "The Case of Poison (Part - 1)": A prominent lawyer is '
                                "found dead in his posh high-end residence.")
    # 2024 tables have a "No. in season" column, used as the episode number
    later, _ = list_page_seasons(parse_tables(fixture("cid_list_2024_present")), rule)
    eps = season_episodes(2024, later[0][1], "year").episodes
    assert [e.marker for e in eps] == ["2024E1", "2024E2"]
    assert "[1]" not in eps[0].summary  # the reference mark is dropped


def test_years_must_rise_down_a_page() -> None:
    rule = run_rule_for(CID)
    html = year_page({2001: (1, 10)}) + year_page({2000: (1, 10)})
    years, skipped = list_page_seasons(parse_tables(html), rule)
    assert [y for y, _ in years] == [2001]
    assert "go back" in skipped[0]["reason"]


@pytest.mark.parametrize(("years", "text"), [
    ([1998, 1999], "1998–1999"), ([2003], "2003"),
    ([1998, 1999, 2001], "1998–1999 and 2001"),
    ([1998, 2000, 2010, 2011], "1998, 2000 and 2010–2011"),
])
def test_format_years(years: list[int], text: str) -> None:
    assert format_years(years) == text


# --- the whole fetch ------------------------------------------------------------------------


def test_years_join_in_order_across_split_pages(cid_plot) -> None:
    rec = cid_plot
    assert rec["fetcher_version"] == FETCHER_VERSION == "1.5.5"
    assert rec["via_detail"] == "episode_table" and rec["series_run_rule"] == "cid_years"
    assert [(p["page_title"], p["source"]["year"]) for p in rec["sources"]] == [
        ("List of CID episodes: 1998–2009", 1998), ("List of CID episodes: 1998–2009", 1999),
        ("List of CID episodes: 2010–2014", 2010), ("List of CID episodes: 2010–2014", 2011),
        ("List of CID episodes: 2024–present", 2024),
        ("List of CID episodes: 2024–present", 2025)]
    assert all("season" not in p["source"] for p in rec["sources"])
    assert rec["sources"][0]["episodes"] == ["1998E1", "1998E2", "1998E3"]
    # pages are read in year order although the main article links them newest first
    fake = cid_fake()
    fetch(fake)
    assert page_requests(fake) ==["1374865182", "1358902654", "1337101470"]
    assert rec["coverage"] == {"unit": "year", "years": [1998, 1999, 2010, 2011, 2024, 2025],
                               "first_year": 1998, "last_year": 2025,
                               "total_basis": "verified_list_headings", "partial": False}
    assert rec["episode_tables"]["unit"] == "year"
    assert all(e["basis"] == "main_article_link" for e in rec["episode_tables"]["evidence"])
    _, params = _request(rec)
    assert _header(params).splitlines()[2] == (
        "Summary covers 1998–1999, 2010–2011 and 2024–2025 of 1998–2025.")


def test_cap_stops_inside_a_year_and_the_line_says_so(partial_plot) -> None:
    rec = partial_plot
    assert [p["source"]["year"] for p in rec["sources"]] == [1998, 1999, 2010]
    report = rec["episode_tables"]
    assert report["stopped_by"] == "word_cap" and report["partial_season"] == 2010
    assert report["pages_not_fetched"] == []  # year headings still set the span:
    assert report["pages_read_for_years_only"] == ["List of CID episodes: 2024–present"]
    assert not any(p["page_title"].endswith("present") for p in rec["sources"])
    assert rec["coverage"]["partial_year"] == 2010 and rec["coverage"]["partial"] is True
    assert rec["word_count"] <= 3000
    gated, params = _request(rec)
    assert _header(params).splitlines()[2] == (
        f"Summary covers 1998–1999 and 2010 of 1998–2025 (2010 only in part{TAIL}")


# --- gate, prompt, verify, record, gold -----------------------------------------------------


def test_verify_request_rebuilds_the_year_line_and_catches_tampering(partial_plot) -> None:
    prompt = load_prompt(DEFAULT_PROMPT_VERSION)
    gated, params = _request(partial_plot)
    verify_request(params, gated, prompt)
    for old, new in ((" (2010 only in part; the summary stops before that year ends)", ""),
                     ("of 1998–2025", "of 1998–2024"), ("1998–1999", "1998"),
                     ("2010 only", "1999 only")):
        bad = copy.deepcopy(params)
        bad["messages"][0]["content"][0]["text"] = _header(bad).replace(old, new)
        with pytest.raises(GateError, match="block 0"):
            verify_request(bad, gated, prompt)


@pytest.mark.parametrize(("change", "match"), [
    (lambda c, s: c.update(years=[1998, 1999]), "doesn't match the sources' years"),
    (lambda c, s: c.update(first_year=1999), "don't hold"),
    (lambda c, s: c.update(last_year=2009), "don't hold"),
    (lambda c, s: c.update(last_year=3000), "don't hold"),
    (lambda c, s: c.update(first_year=1800), "don't hold"),
    (lambda c, s: c.update(first_year="1998"), "don't hold"),
    (lambda c, s: c.update(partial_year=1999), "partial_year"),
    (lambda c, s: c.pop("unit"), "need coverage.unit 'year'"),
    (lambda c, s: s[0]["source"].update(season=1), "season"),
])
def test_malformed_year_coverage_is_refused(partial_plot, change, match) -> None:
    rec = copy.deepcopy(partial_plot)
    change(rec["coverage"], rec["sources"])
    with pytest.raises((InputFormatError, GateError), match=match):
        gate(parse_plot(rec, "t"))


def test_a_prompt_without_the_year_rule_refuses_year_coverage(cid_plot) -> None:
    with pytest.raises(GateError, match="annotate-1.3.0"):
        _request(cid_plot, "annotate-1.2.0")


def test_record_stores_the_year_coverage(partial_plot) -> None:
    from annotate_support import response, valid_output

    from laminary_pipeline.annotate.client import Usage
    from laminary_pipeline.annotate.records import Invalid, RunContext, interpret

    gated = gate(parse_plot(partial_plot, "t"))
    ctx = RunContext(run_id="run_test", model="claude-opus-5-5",
                     prompt_version=DEFAULT_PROMPT_VERSION, effort="medium", batch=False)
    rec = interpret(response(valid_output("breaking_bad")), gated, ctx,
                    usage_so_far=Usage(1000, 2000, 5000, 0), attempts=1)
    assert not isinstance(rec, Invalid), rec
    assert rec["schema_version"] == "1.3.0"
    assert rec["provenance"]["coverage"] == {
        "years": [1998, 1999, 2010], "first_year": 1998, "last_year": 2025,
        "statement": f"Summary covers 1998–1999 and 2010 of 1998–2025 (2010 only in part{TAIL}"}
    assert [s["year"] for s in rec["provenance"]["sources"]] == [1998, 1999, 2010]
    assert validate_record(rec) == []
    bad = copy.deepcopy(rec)
    bad["provenance"]["coverage"]["years"] = [1998, 2010, 1999]
    assert any("years" in e for e in validate_record(bad))
    both = copy.deepcopy(rec)
    both["provenance"]["sources"][0]["season"] = 1
    assert validate_record(both)  # a source has a season or a year, never both


def test_gold_text_and_coverage_equal_the_model_input(partial_plot) -> None:
    gated, params = _request(partial_plot)
    blocks = [b["text"] for b in params["messages"][0]["content"]]
    n = len(partial_plot["sources"])
    joined = "\n\n".join("".join(blocks[1 + 3 * i: 4 + 3 * i]) for i in range(n))
    assert labeler_text(gated) == joined
    assert labeler_coverage(gated) == _header(params).splitlines()[2]
    selection = [{"qid": CID, "title": "CID", "year": 1998, "media_type": "tv_series",
                  "tmdb_id": 1995}]
    rows, skipped = template_rows(selection, {CID: partial_plot})
    assert skipped == []
    assert rows[0]["plot_section"] == "Episode tables (3 years)"
    assert rows[0]["summary_coverage"] == labeler_coverage(gated)
    manifest = manifest_rows(rows, {CID: partial_plot})
    assert manifest[0]["sha256"] == hashlib.sha256(joined.encode("utf-8")).hexdigest()


# --- re-fetch -------------------------------------------------------------------------------


def test_cid_files_from_before_the_year_rule_are_fetched_again(tmp_path: Path) -> None:
    paths = DataPaths.resolve(str(tmp_path))
    old = {"qid": CID, "status": "skipped", "skip_reason": "too_short",
           "fetcher_version": "1.5.4", "candidate": {"media_type": "tv_series"},
           "season_articles": {"used": [], "skipped": []}}
    write_json_atomic(paths.plot_file(CID), old)
    assert needs_fetch(paths, CID, False)
    write_json_atomic(paths.plot_file(CID), {**old, "fetcher_version": FETCHER_VERSION})
    assert not needs_fetch(paths, CID, False)
