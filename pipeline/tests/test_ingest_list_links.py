"""Episode-list page verification fallback (fetcher 1.5.2, DECISIONS 2026-10-02).

When Wikidata doesn't state P179/P361 for an episode-list page, it is accepted if its title is
"List of <series> episodes" (or a split form) and the series' own main article, at the pinned
revision, links to it. Midsomer Murders ("List of Midsomer Murders episodes", Q1092865) and CID
("List of CID episodes: 1998–2009" ..., linked from "CID (Indian TV series)") are the real
cases; the fixtures here are the synthetic "Tidewater (TV series)" ones.
"""

from __future__ import annotations

import urllib.parse

from test_ingest_episodes import (
    SERIES,
    SPLIT_LISTS,
    episode_fake,
    fetch,
    list_page_html,
    page_requests,
    split_list_fake,
)
from test_ingest_seasons import page, sections

from laminary_pipeline.ingest.seasons import LINK_BASIS, list_statements_sparql


def _reasons(rec: dict) -> dict[str, str]:
    return {s["title"]: s["reason"] for s in rec["season_articles"]["skipped"]}


def test_linked_title_matched_list_pages_are_accepted_without_wikidata() -> None:
    """CID's shape: split list pages "List of <X> episodes: <years>" of a main article titled
    "<X> (TV series)", none stated on Wikidata, all linked from the main article."""
    fake = split_list_fake(verified=())
    rec = fetch(fake, seasons_total=None)
    assert rec["status"] == "ok" and rec["via_detail"] == "episode_table"
    assert [p["source"]["season"] for p in rec["sources"]] == [1, 2, 3]
    evidence = rec["episode_tables"]["evidence"]
    assert evidence == [
        {"title": t, "item": SPLIT_LISTS[t][2], "basis": LINK_BASIS, "series": SERIES,
         "linked_from": "Tidewater (TV series)", "linked_from_revision": 92000}
        for t in ("List of Tidewater episodes: 1998–2009",
                  "List of Tidewater episodes: 2010–present")]
    # Wikidata was asked whether the pages belong to another series
    queries = [dict(urllib.parse.parse_qsl((r.data or b"").decode())).get("query", "")
               for r in fake.requests]
    assert any("# laminary list-page statements" in q for q in queries)


def test_an_unlinked_list_page_is_still_rejected() -> None:
    """Midsomer Murders' shape, but without the link: the plain list page is only found by its
    guessed title, so it stays unverified."""
    fake = episode_fake(list_html=list_page_html({1: (3, 60), 2: (3, 60)}))
    fake.data["sparql"]["season_check:" + SERIES] = {"results": {"bindings": []}}
    rec = fetch(fake)
    assert rec["status"] == "skipped"
    assert _reasons(rec)["List of Tidewater episodes"] == (
        "unverified: Wikidata item Q9200010 is not stated as part of Q9200000 (P179/P361) and "
        "the main article doesn't link to it")
    assert "92100" not in page_requests(fake)


def test_the_same_list_page_is_accepted_once_the_main_article_links_it() -> None:
    fake = episode_fake(list_html=list_page_html({1: (3, 60), 2: (3, 60)}))
    fake.data["sparql"]["season_check:" + SERIES] = {"results": {"bindings": []}}
    fake.data["wikipedia"]["links:92000"] = {"parse": {"links": [
        {"ns": 0, "title": "List of Tidewater episodes", "exists": True}]}}
    rec = fetch(fake)
    assert rec["status"] == "ok"
    assert {p["page_title"] for p in rec["sources"]} == {"List of Tidewater episodes"}
    assert rec["episode_tables"]["evidence"][0]["basis"] == LINK_BASIS


def test_a_linked_page_whose_title_does_not_match_is_rejected() -> None:
    """The link "List of Tidewater episodes" redirects to a page with another title, and the
    main article also links another show's list page: neither is used."""
    fake = episode_fake()
    w = fake.data["wikipedia"]
    w["links:92000"] = {"parse": {"links": [
        {"ns": 0, "title": "List of Tidewater episodes", "exists": True},
        {"ns": 0, "title": "List of Harbor Lights episodes", "exists": True}]}}
    w["query:List of Tidewater episodes"] = page("Tidewater episode guide", 9210, 92100,
                                                 "Q9200010",
                                                 redirect_from="List of Tidewater episodes")
    w["query:List of Harbor Lights episodes"] = page("List of Harbor Lights episodes", 9211,
                                                     92110, "Q9200011")
    w["sections:92100"] = sections("Tidewater episode guide", ["Series overview"])
    w["page:92100"] = {"parse": {"text": list_page_html({1: (3, 60)})}}
    w["page:92110"] = {"parse": {"text": list_page_html({1: (3, 60)})}}
    fake.data["sparql"]["season_check:" + SERIES] = {"results": {"bindings": []}}
    rec = fetch(fake)
    assert rec["status"] == "skipped"
    assert _reasons(rec)["Tidewater episode guide"] == "not a season or episode-list page"
    assert "List of Harbor Lights episodes" not in _reasons(rec)  # never even considered
    assert not {"92100", "92110"} & set(page_requests(fake))


def test_wikidata_naming_another_series_still_rejects_a_linked_list_page() -> None:
    fake = split_list_fake(verified=())
    fake.data["sparql"]["list_statements"] = {"results": {"bindings": [
        {"item": {"value": f"http://www.wikidata.org/entity/{SPLIT_LISTS[t][2]}"},
         "prop": {"value": "P361"}, "value": {"value": "http://www.wikidata.org/entity/Q5"}}
        for t in SPLIT_LISTS]}}
    rec = fetch(fake, seasons_total=None)
    assert rec["status"] == "skipped"
    for title in SPLIT_LISTS:
        assert _reasons(rec)[title] == (
            f"unverified: Wikidata places item {SPLIT_LISTS[title][2]} in another series "
            "(P361 Q5), not Q9200000")


def test_a_statement_for_the_series_itself_is_not_another_series() -> None:
    """The statements query returns every P179/P361 value; the series' own QID is not
    'another series' (it would have verified the page in the first place)."""
    fake = split_list_fake(verified=())
    fake.data["sparql"]["list_statements"] = {"results": {"bindings": [
        {"item": {"value": "http://www.wikidata.org/entity/Q9200030"},
         "prop": {"value": "P361"},
         "value": {"value": f"http://www.wikidata.org/entity/{SERIES}"}}]}}
    rec = fetch(fake, seasons_total=None)
    assert rec["status"] == "ok"


def test_season_pages_get_no_link_fallback() -> None:
    """Tidewater season 3 is unverified on Wikidata; a link from the main article doesn't
    change that."""
    from test_ingest_episodes import TWO_SEASONS, season_page_html

    pages = {**TWO_SEASONS, 3: season_page_html(3, 3, 40)}
    fake = episode_fake(season_pages=pages)
    fake.data["wikipedia"]["links:92000"] = {"parse": {"links": [
        {"ns": 0, "title": "Tidewater season 3", "exists": True}]}}
    rec = fetch(fake)
    assert [p["source"]["season"] for p in rec["sources"]] == [1, 2, 4]
    assert _reasons(rec)["Tidewater season 3"] == (
        "unverified: Wikidata item Q9200003 is not stated as part of Q9200000 (P179/P361)")


def test_list_statements_query_refuses_bad_ids() -> None:
    import pytest

    with pytest.raises(ValueError):
        list_statements_sparql(["Q1", "wd:Q2 } ?x"])


def test_series_with_an_unverified_list_page_are_fetched_again_by_1_5_2(tmp_path) -> None:
    from laminary_pipeline.ingest.paths import DataPaths, write_json_atomic
    from laminary_pipeline.ingest.plots import needs_fetch

    paths = DataPaths.resolve(str(tmp_path))
    old = {"qid": "Q751917", "status": "skipped", "skip_reason": "too_short",
           "candidate": {"media_type": "tv_series"}, "fetcher_version": "1.5.1",
           "season_articles": {"used": [], "skipped": [
               {"title": "List of Midsomer Murders episodes",
                "reason": "unverified: Wikidata item Q1092865 is not stated as part of "
                          "Q751917 (P179/P361)"}]}}
    write_json_atomic(paths.plot_file("Q751917"), old)
    assert needs_fetch(paths, "Q751917", False)
    write_json_atomic(paths.plot_file("Q751917"), {**old, "fetcher_version": "1.5.2"})
    assert not needs_fetch(paths, "Q751917", False)
    # an unverified season page alone doesn't trigger it
    season_only = {**old, "season_articles": {"used": [], "skipped": [
        {"title": "Midsomer Murders series 3", "reason": "unverified: ..."}]}}
    write_json_atomic(paths.plot_file("Q751917"), season_only)
    assert not needs_fetch(paths, "Q751917", False)
