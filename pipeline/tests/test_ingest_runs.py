"""Per-title series run rules (fetcher 1.5.1, DECISIONS 2026-10-02): Doctor Who revival only.

Synthetic fixtures only: none of Doctor Who's season or episode-list pages were in the local
HTTP cache. The page titles follow English Wikipedia's (main article revision 1377232352 links
"Doctor Who season 8", "Doctor Who series 11" ... "series 15", "List of Doctor Who episodes
(1963–1989)" and "(2005–present)"); the text is synthetic words and the Wikidata items are
made up.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_ingest_episodes import ep_rows, heading, page_requests, table
from test_ingest_seasons import NOW, page, section_html, sections, words
from wikimedia_fake import FakeWikimedia

from laminary_pipeline.annotate.inputs import parse_plot
from laminary_pipeline.ingest import runs
from laminary_pipeline.ingest.episodes import list_page_seasons, parse_tables
from laminary_pipeline.ingest.http import HttpClient
from laminary_pipeline.ingest.paths import DataPaths, write_json_atomic
from laminary_pipeline.ingest.plots import needs_fetch
from laminary_pipeline.ingest.priority import PRIORITY_SERIES
from laminary_pipeline.ingest.runs import SERIES_RUN_RULES, run_rule_for
from laminary_pipeline.ingest.seasons import (
    episode_list_order,
    season_coverage,
    season_number,
    season_numbering,
)
from laminary_pipeline.ingest.wikipedia import FETCHER_VERSION, PlotFetcher

DW = "Q34316"
MAIN = "Doctor Who"
CLASSIC_LIST = "List of Doctor Who episodes (1963–1989)"
REVIVAL_LIST = "List of Doctor Who episodes (2005–present)"
# title -> (pageid, revid, Wikidata item, P179 ordinal or None for P361)
CLASSIC = {
    "Doctor Who season 1": (3401, 340010, "Q9340001", 1),
    "Doctor Who season 2": (3402, 340020, "Q9340002", 2),
    "Doctor Who season 26": (3426, 340260, "Q9340026", 26),  # linked from the main article
}
REVIVAL = {
    "Doctor Who series 1": (3501, 350010, "Q9350001", 1),
    "Doctor Who series 2": (3502, 350020, "Q9350002", 2),
    "Doctor Who series 3": (3503, 350030, "Q9350003", 3),  # no plot section
}
LISTS = {
    CLASSIC_LIST: (3600, 360000, "Q9360000", None),
    REVIVAL_LIST: (3605, 360500, "Q9360005", None),
}


def list_html(headings: list[tuple[str, int, int]]) -> str:
    """An episode-list page: one episode table (3 episodes, ``size`` words each) per heading,
    as (heading text, season number used in the synthetic summaries, words per summary)."""
    body = ['<div class="mw-content-ltr mw-parser-output">', heading(2, "Episodes")]
    overall = 1
    for text, n, size in headings:
        body += [heading(3, text), table(ep_rows(n, 3, size, first_overall=overall))]
        overall += 3
    return "".join(body + ["</div>"])


CLASSIC_HEADINGS = [("Season 1 (1963–64)", 1, 40), ("Season 2 (1964–65)", 2, 40),
                    ("Season 3 (1965–66)", 3, 40)]
REVIVAL_HEADINGS = [("Series 1 (2005)", 1, 40), ("Series 2 (2006)", 2, 40),
                    ("Season 1 (2024)", 1, 40)]


def dw_fake(
    *, prose: bool = True, revival_headings: list[tuple[str, int, int]] | None = None,
    verified: tuple[str, ...] | None = None,
) -> FakeWikimedia:
    """Doctor Who with a thin main article. Classic "season" pages and revival "series" pages,
    both numbered from 1 and all stated on Wikidata as part of Q34316 (P179 with ordinals),
    unless ``verified`` lists the only items that are. With ``prose`` every season page but
    "series 3" has a 600-word Plot section; without it they have none (and no episode table),
    so the episode-list pages are tried. Both list pages have episode summaries."""
    fake = FakeWikimedia()
    w = fake.data["wikipedia"]
    w[f"query:{MAIN}"] = page(MAIN, 3400, 340000, DW)
    w["sections:340000"] = sections(MAIN, ["Premise", "Episodes"])
    w["text:340000:1"] = section_html("Premise", words(40, "premise"))
    w["text:340000:2"] = section_html("Episodes", "", table=True)
    w["links:340000"] = {"parse": {"links": [
        {"ns": 0, "title": t, "exists": True}
        for t in ("Doctor Who season 26", "Doctor Who series 2", CLASSIC_LIST, REVIVAL_LIST)]}}
    for title, (pid, rev, item, _) in {**CLASSIC, **REVIVAL}.items():
        w[f"query:{title}"] = page(title, pid, rev, item)
        has_plot = prose and title != "Doctor Who series 3"
        w[f"sections:{rev}"] = sections(title, ["Plot", "Cast"] if has_plot else ["Cast"])
        if has_plot:
            tag = "c" if "season" in title else "r"
            w[f"text:{rev}:1"] = section_html("Plot", words(600, f"{tag}{title[-2:].strip()}w"))
    for title, (pid, rev, item, _) in LISTS.items():
        w[f"query:{title}"] = page(title, pid, rev, item)
        w[f"sections:{rev}"] = sections(title, ["Series overview"])
        w[f"text:{rev}:1"] = section_html("Series overview", "", table=True)
    w["page:360000"] = {"parse": {"text": list_html(CLASSIC_HEADINGS)}}
    w["page:360500"] = {"parse": {"text": list_html(revival_headings or REVIVAL_HEADINGS)}}
    bindings = [
        {"item": {"value": f"http://www.wikidata.org/entity/{item}"},
         **({"ordinal": {"value": str(o)}} if o else {})}
        for _, _, item, o in {**CLASSIC, **REVIVAL, **LISTS}.values()
        if verified is None or item in verified
    ]
    fake.data["sparql"]["season_check:" + DW] = {"results": {"bindings": bindings}}
    return fake


def fetch(fake: FakeWikimedia, *, number_of_seasons: int | None = 40) -> dict[str, Any]:
    client = HttpClient(fake, None, sleep=lambda s: None, min_interval={})
    cand = {"qid": DW, "enwiki_title": MAIN, "media_type": "tv_series", "title": "Doctor Who",
            "year": 1963, "tmdb_id": 121, "series_status": "unknown",
            "series_status_basis": "none", "number_of_seasons": number_of_seasons}
    return PlotFetcher(client, clock=lambda: NOW).fetch(cand)


def fetched_revs(fake: FakeWikimedia) -> set[str]:
    """Every revision the fetcher parsed (sections, section text or whole page)."""
    import urllib.parse

    out = set()
    for r in fake.requests:
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.url).query))
        if params.get("action") == "parse" and params.get("prop") != "links":
            out.add(params["oldid"])
    return out


CLASSIC_REVS = {str(v[1]) for v in CLASSIC.values()} | {"360000"}


# --- the rule table ---------------------------------------------------------------------------


def test_doctor_who_has_the_revival_rule_and_is_a_priority_series() -> None:
    rule = run_rule_for(DW)
    assert rule is not None and rule.name == "doctor_who_revival"
    assert (rule.numbering, rule.list_pages_from) == ("series", 2005)
    assert PRIORITY_SERIES[DW] == "Doctor Who"
    assert list(SERIES_RUN_RULES) == [DW]  # an explicit per-title table, not a heuristic
    assert run_rule_for("Q23733") is None  # Seinfeld: no rule


@pytest.mark.parametrize(("title", "numbering", "number"), [
    ("Doctor Who series 4", "series", 4),
    ("Doctor Who (series 1)", "series", 1),
    ("Doctor Who season 26", "season", 26),
    ("Doctor Who (season 3)", "season", 3),
    ("Doctor Who season 1 (2024)", None, None),  # a relaunch title that restarts: not a page
    ("Doctor Who Confidential", None, None),
])
def test_season_page_numbering(title: str, numbering: str | None, number: int | None) -> None:
    assert season_numbering(title, MAIN) == numbering
    assert season_number(title, MAIN) == number


@pytest.mark.parametrize(("title", "ok"), [
    (REVIVAL_LIST, True),
    ("List of Doctor Who episodes (2023–present)", True),  # used only if seasons keep rising
    (CLASSIC_LIST, False),
    ("List of Doctor Who episodes", False),  # could hold either run
    ("List of Doctor Who episodes (seasons 1–5)", False),
    ("List of Doctor Who episodes (part 2)", False),
])
def test_rule_allows_only_revival_year_range_list_pages(title: str, ok: bool) -> None:
    rule = run_rule_for(DW)
    assert rule is not None
    assert rule.list_page_ok(episode_list_order(title, MAIN, MAIN)) is ok


def test_rule_skips_season_headings_on_a_list_page() -> None:
    rule = run_rule_for(DW)
    tables = parse_tables(list_html(REVIVAL_HEADINGS))
    seasons, skipped = list_page_seasons(tables, rule)
    assert [n for n, _ in seasons] == [1, 2]
    assert skipped == [{"heading": "season 1 (2024)", "reason": rule.heading_reason()}]
    # without the rule the same "Season 1" heading is a restart that stops the page
    seasons, skipped = list_page_seasons(tables)
    assert [n for n, _ in seasons] == [1, 2] and "restarts" in skipped[0]["reason"]


def test_coverage_without_wikidata_total_counts_the_run_only() -> None:
    cov = season_coverage([1, 2], [1, 2, 3], 40, use_wikidata_total=False)
    assert cov == {"seasons": [1, 2], "total_seasons": 3,
                   "total_seasons_basis": "verified_season_pages",
                   "wikidata_number_of_seasons_ignored": 40, "partial": True}
    # the default is unchanged: P2437 when it is at least every verified page
    assert season_coverage([1, 2], [1, 2, 3], 40)["total_seasons_basis"] == "wikidata_P2437"


# --- season-article prose -------------------------------------------------------------------


def test_revival_series_pages_are_used_and_classic_pages_ignored() -> None:
    fake = dw_fake()
    rec = fetch(fake)
    assert rec["fetcher_version"] == FETCHER_VERSION == "1.5.1"
    assert rec["status"] == "ok" and rec["via"] == "season_articles"
    assert "via_detail" not in rec
    assert [(p["page_title"], p["source"]["season"]) for p in rec["sources"]] == [
        ("Doctor Who series 1", 1), ("Doctor Who series 2", 2)]
    assert rec["sources"][0]["text"].startswith("r1w0 ")
    assert rec["series_run_rule"] == "doctor_who_revival"
    rule = run_rule_for(DW)
    assert {i["title"]: i["reason"] for i in rec["series_run_ignored"]} == {
        **{t: rule.season_page_reason() for t in CLASSIC},
        CLASSIC_LIST: rule.list_page_reason()}
    report = rec["season_articles"]
    assert report["verified_seasons"] == [1, 2, 3]  # revival series only
    assert not any("two pages claim" in s["reason"] for s in report["skipped"])
    assert [e["item"] for e in report["evidence"]] == ["Q9350001", "Q9350002"]
    assert all(e["series"] == DW and e["property"] == "P179" for e in report["evidence"])
    # "of N" counts revival series only: not Wikidata's 40, not classic season 26
    assert rec["coverage"] == {"seasons": [1, 2], "total_seasons": 3,
                               "total_seasons_basis": "verified_season_pages",
                               "wikidata_number_of_seasons_ignored": 40, "partial": True}
    assert not (fetched_revs(fake) & CLASSIC_REVS)  # classic pages are never read
    parse_plot(rec, "t")  # valid annotation input


def test_without_the_rule_the_two_runs_block_each_other(monkeypatch) -> None:
    """The behaviour the rule fixes: "season 1" and "series 1" both claim season 1."""
    monkeypatch.delitem(runs.SERIES_RUN_RULES, DW)
    rec = fetch(dw_fake())
    reasons = [s["reason"] for s in rec["season_articles"]["skipped"]]
    assert "two pages claim season 1" in reasons and "two pages claim season 2" in reasons
    assert "series_run_rule" not in rec
    assert [p["page_title"] for p in rec["sources"]] == ["Doctor Who season 26"]


# --- episode tables -------------------------------------------------------------------------


def test_episode_tables_use_the_revival_list_page_only() -> None:
    fake = dw_fake(prose=False)
    rec = fetch(fake)
    assert rec["status"] == "ok" and rec["via_detail"] == "episode_table"
    assert [(p["page_title"], p["source"]["season"]) for p in rec["sources"]] == [
        (REVIVAL_LIST, 1), (REVIVAL_LIST, 2)]
    report = rec["episode_tables"]
    assert report["page_kind"] == "episode_list_page"
    assert report["pages_used"] == [REVIVAL_LIST]
    assert report["numbering_restart"] == []  # the classic run no longer blocks the revival
    assert {"title": REVIVAL_LIST, "heading": "season 1 (2024)",
            "reason": run_rule_for(DW).heading_reason()} in report["tables_skipped"]
    assert "360000" not in page_requests(fake)  # the classic list page is never fetched
    assert not (fetched_revs(fake) & CLASSIC_REVS)
    assert CLASSIC_LIST in {i["title"] for i in rec["series_run_ignored"]}
    assert rec["coverage"]["total_seasons"] == 3  # series pages 1-3 (verified), not 40 or 26
    assert rec["coverage"]["total_seasons_basis"] == "verified_season_pages"
    assert rec["coverage"]["wikidata_number_of_seasons_ignored"] == 40
    parse_plot(rec, "t")


def test_list_headings_count_for_the_total_and_continuing_series_are_kept() -> None:
    """Series 14 and 15 (the 2024 relaunch, as English Wikipedia numbers it now) continue the
    revival numbering, so they are used and counted."""
    heads = [("Series 1 (2005)", 1, 40), ("Series 14 (2024)", 14, 40),
             ("Series 15 (2025)", 15, 40)]
    rec = fetch(dw_fake(prose=False, revival_headings=heads), number_of_seasons=None)
    assert [p["source"]["season"] for p in rec["sources"]] == [1, 14, 15]
    assert rec["coverage"]["total_seasons"] == 15
    assert "wikidata_number_of_seasons_ignored" not in rec["coverage"]


def test_a_relaunch_restarting_at_series_1_is_out_of_scope() -> None:
    heads = [("Series 1 (2005)", 1, 40), ("Series 13 (2021)", 13, 40),
             ("Series 1 (2024)", 1, 40)]
    rec = fetch(dw_fake(prose=False, revival_headings=heads))
    assert [p["source"]["season"] for p in rec["sources"]] == [1, 13]
    skipped = rec["episode_tables"]["tables_skipped"]
    assert any(t["heading"] == "series 1 (2024)" and "restarts" in t["reason"]
               for t in skipped)
    assert rec["coverage"]["total_seasons"] == 13


def test_verification_is_not_loosened_for_the_rule() -> None:
    """If the revival pages are items of a separate revival series (not stated as part of
    Q34316), they are unverified and skipped; the classic pages are still not used."""
    classic_only = (*(v[2] for v in CLASSIC.values()), LISTS[CLASSIC_LIST][2])
    rec = fetch(dw_fake(verified=classic_only))
    assert rec["status"] == "skipped"
    assert rec["series_run_rule"] == "doctor_who_revival"
    reasons = {s["title"]: s["reason"] for s in rec["season_articles"]["skipped"]}
    for title in (*REVIVAL, REVIVAL_LIST):
        assert reasons[title].startswith("unverified")
    assert rec["season_articles"]["verified_seasons"] == []
    assert not any(t in reasons for t in CLASSIC)  # ignored by the rule, not verified


# --- re-fetch ---------------------------------------------------------------------------------


def test_doctor_who_skipped_by_an_older_fetcher_is_fetched_again(tmp_path: Path) -> None:
    paths = DataPaths.resolve(str(tmp_path))
    old = {"qid": DW, "status": "skipped", "skip_reason": "too_short",
           "candidate": {"media_type": "tv_series"}, "season_articles": {"used": []},
           "fetcher_version": "1.5.0"}
    write_json_atomic(paths.plot_file(DW), old)
    assert needs_fetch(paths, DW, False)
    write_json_atomic(paths.plot_file(DW), {**old, "fetcher_version": "1.5.1"})
    assert not needs_fetch(paths, DW, False)
    # other series are unaffected by the rule's version
    write_json_atomic(paths.plot_file("Q1"), {**old, "qid": "Q1"})
    assert not needs_fetch(paths, "Q1", False)
