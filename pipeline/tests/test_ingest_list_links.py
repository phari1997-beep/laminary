"""Episode-list page verification fallback (fetcher 1.5.2, hardened in 1.5.3; DECISIONS
2026-10-02).

When Wikidata doesn't state P179/P361 for an episode-list page, it is accepted if its title is
"List of <series> episodes" (or a split form), the series' own main article, at the pinned
revision, links to it, and (1.5.3, QA) the list page, at its fetched revision, links back to the
main article (its title or a redirect to it). Only a P179/P361 to a television series rejects
it. Midsomer Murders ("List of Midsomer Murders episodes", Q1092865) and CID ("List of CID
episodes: 1998–2009" ..., linked from "CID (Indian TV series)") are the real cases; the
fixtures here are the synthetic "Tidewater (TV series)" ones.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

import pytest
from test_ingest_episodes import (
    MAIN,
    SERIES,
    SPLIT_LISTS,
    episode_fake,
    fetch,
    list_page_html,
    page_requests,
    split_list_fake,
)
from test_ingest_seasons import page, sections

from laminary_pipeline.ingest.seasons import (
    LINK_BASIS,
    TELEVISION_SERIES,
    list_statements_sparql,
)
from laminary_pipeline.ingest.wikipedia import FETCHER_VERSION

LIST = "List of Tidewater episodes"
LIST_REV = "92100"


def _reasons(rec: dict) -> dict[str, str]:
    return {s["title"]: s["reason"] for s in rec["season_articles"]["skipped"]}


def _links(*titles: str) -> dict[str, Any]:
    return {"parse": {"links": [{"ns": 0, "title": t, "exists": True} for t in titles]}}


def _statement(item: str, value: str, *, tv: bool, prop: str = "P361") -> dict[str, Any]:
    return {"item": {"value": f"http://www.wikidata.org/entity/{item}"}, "prop": {"value": prop},
            "value": {"value": f"http://www.wikidata.org/entity/{value}"},
            "tv": {"datatype": "http://www.w3.org/2001/XMLSchema#boolean",
                   "value": "true" if tv else "false"}}


def plain_list_fake(*, item: str | None = "Q9200010", back: tuple[str, ...] = (MAIN,),
                    main_links: tuple[str, ...] = (LIST,)) -> Any:
    """Midsomer Murders' shape: the plain "List of Tidewater episodes" page, no Wikidata
    statement, linked from the main article and (``back``) linking back to it."""
    fake = episode_fake(list_html=list_page_html({1: (3, 60), 2: (3, 60)}))
    w = fake.data["wikipedia"]
    fake.data["sparql"]["season_check:" + SERIES] = {"results": {"bindings": []}}
    w["links:92000"] = _links(*main_links)
    w[f"links:{LIST_REV}:0"] = _links(*back)  # the lead's links
    if item is None:
        w[f"query:{LIST}"] = page(LIST, 9210, 92100, None)
    return fake


# --- accepted ---------------------------------------------------------------------------------


def test_linked_split_list_pages_that_link_back_are_accepted() -> None:
    """CID's shape: split list pages "List of <X> episodes: <years>" of a main article titled
    "<X> (TV series)", none stated on Wikidata, linked from the main article and back."""
    fake = split_list_fake(verified=())
    rec = fetch(fake, seasons_total=None)
    assert rec["status"] == "ok" and rec["via_detail"] == "episode_table"
    assert [p["source"]["season"] for p in rec["sources"]] == [1, 2, 3]
    assert rec["episode_tables"]["evidence"] == [
        {"title": t, "item": SPLIT_LISTS[t][2], "basis": LINK_BASIS, "series": SERIES,
         "linked_from": MAIN, "linked_from_revision": 92000, "links_back_via": MAIN,
         "list_revision": SPLIT_LISTS[t][1], "other_statements": []}
        for t in ("List of Tidewater episodes: 1998–2009",
                  "List of Tidewater episodes: 2010–present")]
    # Wikidata was asked whether the pages belong to another television series
    queries = [dict(urllib.parse.parse_qsl((r.data or b"").decode())).get("query", "")
               for r in fake.requests]
    assert any("# laminary list-page statements" in q and TELEVISION_SERIES in q
               and "Q15416" in q for q in queries)  # television program counts too


def test_plain_list_page_linked_both_ways_is_accepted() -> None:
    rec = fetch(plain_list_fake())
    assert rec["status"] == "ok"
    assert {p["page_title"] for p in rec["sources"]} == {LIST}
    ev = rec["episode_tables"]["evidence"][0]
    assert (ev["basis"], ev["links_back_via"], ev["list_revision"]) == (LINK_BASIS, MAIN, 92100)


def test_main_article_linking_through_a_redirect_is_accepted() -> None:
    """The main article links "Tidewater episode list", which redirects to the list page
    (``_linked``); the list page links back through a redirect to the main article."""
    fake = plain_list_fake(back=("Tidewater (1989 TV series)",), main_links=())
    w = fake.data["wikipedia"]
    w["links:92000"] = _links("List of Tidewater episodes (part 1)")
    w["query:List of Tidewater episodes (part 1)"] = page(
        LIST, 9210, 92100, "Q9200010", redirect_from="List of Tidewater episodes (part 1)")
    w[f"redirects:{MAIN}"] = {"batchcomplete": True, "query": {"pages": [
        {"pageid": 9200, "ns": 0, "title": MAIN,
         "redirects": [{"pageid": 1, "ns": 0, "title": "Tidewater (1989 TV series)"}]}]}}
    rec = fetch(fake)
    assert rec["status"] == "ok"
    ev = rec["episode_tables"]["evidence"][0]
    assert ev["basis"] == LINK_BASIS and ev["links_back_via"] == "Tidewater (1989 TV series)"


def test_linked_list_page_without_a_wikidata_item_is_accepted() -> None:
    fake = plain_list_fake(item=None)
    rec = fetch(fake)
    assert rec["status"] == "ok"
    ev = rec["episode_tables"]["evidence"][0]
    assert ev["item"] is None and ev["basis"] == LINK_BASIS
    # nothing to ask Wikidata about for an item-less page
    queries = [dict(urllib.parse.parse_qsl((r.data or b"").decode())).get("query", "")
               for r in fake.requests]
    assert not any("# laminary list-page statements" in q for q in queries)


def test_a_statement_to_something_that_is_not_a_tv_series_does_not_reject() -> None:
    fake = plain_list_fake()
    fake.data["sparql"]["list_statements"] = {"results": {"bindings": [
        _statement("Q9200010", "Q13406463", tv=False)]}}  # e.g. a Wikimedia list article class
    rec = fetch(fake)
    assert rec["status"] == "ok"
    assert rec["episode_tables"]["evidence"][0]["other_statements"] == ["P361 Q13406463"]


def test_a_statement_for_the_series_itself_is_not_another_series() -> None:
    fake = split_list_fake(verified=())
    fake.data["sparql"]["list_statements"] = {"results": {"bindings": [
        _statement("Q9200030", SERIES, tv=True)]}}
    rec = fetch(fake, seasons_total=None)
    assert rec["status"] == "ok"


# --- rejected ---------------------------------------------------------------------------------


def test_remake_cannot_take_the_original_shows_list_page() -> None:
    """Gossip Girl's shape: the remake "Tidewater (TV series)" links "List of Tidewater
    episodes", which belongs to the original show and links only to the original's article.
    Wikidata states nothing, so only the missing back-link stops it."""
    fake = plain_list_fake(back=("Tidewater (1989 TV series)", "Harbor Lights"))
    rec = fetch(fake)
    assert rec["status"] == "skipped"
    assert _reasons(rec)[LIST] == (
        "unverified: Wikidata item Q9200010 is not stated as part of Q9200000 (P179/P361), and "
        f"the page doesn't link back to {MAIN!r}")
    assert LIST_REV not in page_requests(fake)  # its tables are never read


def test_an_unlinked_list_page_is_still_rejected() -> None:
    """The plain list page is only found by its guessed title, so it stays unverified."""
    fake = plain_list_fake(main_links=())
    rec = fetch(fake)
    assert rec["status"] == "skipped"
    assert _reasons(rec)[LIST] == (
        "unverified: Wikidata item Q9200010 is not stated as part of Q9200000 (P179/P361) and "
        "the main article doesn't link to it")
    assert LIST_REV not in page_requests(fake)


def test_a_linked_page_whose_title_does_not_match_is_rejected() -> None:
    """The link "List of Tidewater episodes" redirects to a page with another title, and the
    main article also links another show's list page: neither is used."""
    fake = episode_fake()
    w = fake.data["wikipedia"]
    w["links:92000"] = _links(LIST, "List of Harbor Lights episodes")
    w[f"query:{LIST}"] = page("Tidewater episode guide", 9210, 92100, "Q9200010",
                              redirect_from=LIST)
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


def test_a_differently_cased_series_name_is_not_a_match() -> None:
    """Titles match case-sensitively after the first letter: "List of TideWater episodes" is
    another page. It links both ways and has episode tables, so before fetcher 1.5.3
    (case-insensitive) it would have been used."""
    from laminary_pipeline.ingest.seasons import episode_list_order

    assert episode_list_order("List of TideWater episodes", "Tidewater", MAIN) is None
    assert episode_list_order("list of Tidewater episodes", "Tidewater", MAIN) == (0, 0)
    fake = plain_list_fake(main_links=("List of TideWater episodes",))
    w = fake.data["wikipedia"]
    w["query:List of TideWater episodes"] = page("List of TideWater episodes", 9212, 92120,
                                                 "Q9200012")
    w["sections:92120"] = sections("List of TideWater episodes", ["Series overview"])
    w["text:92120:1"] = {"parse": {"text": "<div><table><tr><td>1</td></tr></table></div>"}}
    w["links:92120:0"] = _links(MAIN)
    w["page:92120"] = {"parse": {"text": list_page_html({1: (3, 60), 2: (3, 60)})}}
    rec = fetch(fake)
    assert rec["status"] == "skipped"
    assert "92120" not in page_requests(fake)


def test_a_back_link_outside_the_lead_does_not_count() -> None:
    """QA: La Femme Nikita's list page links the remake "Nikita (TV series)" only in its
    navbox, Mr. Bean's links the animated series in its body. Only the lead's links (section
    0) are asked for, so the remake is rejected."""
    fake = plain_list_fake(back=("Harbor Lights",))  # the lead links only another show
    fake.data["wikipedia"][f"links:{LIST_REV}"] = _links(MAIN, "Harbor Lights")  # whole page
    rec = fetch(fake)
    assert rec["status"] == "skipped"
    assert _reasons(rec)[LIST].endswith(f"the page doesn't link back to {MAIN!r}")
    lead_requests = [r.url for r in fake.requests
                     if "prop=links" in r.url and f"oldid={LIST_REV}" in r.url]
    assert lead_requests and all("section=0" in u for u in lead_requests)


def test_redirect_lookups_follow_continuation() -> None:
    """A main article with more than 500 redirects: the back-link through a redirect on the
    second page of ``prop=redirects`` still counts."""
    fake = plain_list_fake(back=("Tidewater (1989 TV series)",))
    w = fake.data["wikipedia"]
    w[f"redirects:{MAIN}"] = {"continue": {"rdcontinue": "9200|1"}, "query": {"pages": [
        {"pageid": 9200, "ns": 0, "title": MAIN, "redirects": [{"title": "Tidewater show"}]}]}}
    w[f"redirects:{MAIN}:9200|1"] = {"batchcomplete": True, "query": {"pages": [
        {"pageid": 9200, "ns": 0, "title": MAIN,
         "redirects": [{"title": "Tidewater (1989 TV series)"}]}]}}
    rec = fetch(fake)
    assert rec["status"] == "ok"
    assert rec["episode_tables"]["evidence"][0]["links_back_via"] == "Tidewater (1989 TV series)"


def test_wikidata_naming_another_tv_series_still_rejects_a_linked_list_page() -> None:
    fake = split_list_fake(verified=())
    fake.data["sparql"]["list_statements"] = {"results": {"bindings": [
        _statement(SPLIT_LISTS[t][2], "Q5", tv=True) for t in SPLIT_LISTS]}}
    rec = fetch(fake, seasons_total=None)
    assert rec["status"] == "skipped"
    for title in SPLIT_LISTS:
        assert _reasons(rec)[title] == (
            f"unverified: Wikidata places item {SPLIT_LISTS[title][2]} in another television "
            "series (P361 Q5), not Q9200000")


def test_season_pages_get_no_link_fallback() -> None:
    """Tidewater season 3 is unverified on Wikidata; links both ways don't change that."""
    from test_ingest_episodes import TWO_SEASONS, season_page_html

    pages = {**TWO_SEASONS, 3: season_page_html(3, 3, 40)}
    fake = episode_fake(season_pages=pages)
    fake.data["wikipedia"]["links:92000"] = _links("Tidewater season 3")
    fake.data["wikipedia"]["links:92030:0"] = _links(MAIN)
    rec = fetch(fake)
    assert [p["source"]["season"] for p in rec["sources"]] == [1, 2, 4]
    assert _reasons(rec)["Tidewater season 3"] == (
        "unverified: Wikidata item Q9200003 is not stated as part of Q9200000 (P179/P361)")


def test_list_statements_query_refuses_bad_ids() -> None:
    with pytest.raises(ValueError):
        list_statements_sparql(["Q1", "wd:Q2 } ?x"])


# --- re-fetch ---------------------------------------------------------------------------------


def _write(paths: Any, qid: str, rec: dict[str, Any]) -> None:
    from laminary_pipeline.ingest.paths import write_json_atomic

    write_json_atomic(paths.plot_file(qid), rec)


def test_series_with_an_unverified_list_page_are_fetched_again_by_1_5_2(tmp_path) -> None:
    from laminary_pipeline.ingest.paths import DataPaths
    from laminary_pipeline.ingest.plots import needs_fetch

    paths = DataPaths.resolve(str(tmp_path))
    old = {"qid": "Q751917", "status": "skipped", "skip_reason": "too_short",
           "candidate": {"media_type": "tv_series"}, "fetcher_version": "1.5.1",
           "season_articles": {"used": [], "skipped": [
               {"title": "List of Midsomer Murders episodes",
                "reason": "unverified: Wikidata item Q1092865 is not stated as part of "
                          "Q751917 (P179/P361)"}]}}
    _write(paths, "Q751917", old)
    assert needs_fetch(paths, "Q751917", False)
    _write(paths, "Q751917", {**old, "fetcher_version": "1.5.3"})
    assert not needs_fetch(paths, "Q751917", False)
    # an unverified season page alone doesn't trigger it
    season_only = {**old, "season_articles": {"used": [], "skipped": [
        {"title": "Midsomer Murders series 3", "reason": "unverified: ..."}]}}
    _write(paths, "Q751917", season_only)
    assert not needs_fetch(paths, "Q751917", False)


def test_link_verified_records_from_1_5_2_are_fetched_again(tmp_path) -> None:
    """Fetcher 1.5.3 adds the back-link check: a 1.5.2 file built from a link-verified list
    page, or skipped because of a statement to any other item, is fetched again."""
    from laminary_pipeline.ingest.paths import DataPaths
    from laminary_pipeline.ingest.plots import needs_fetch

    paths = DataPaths.resolve(str(tmp_path))
    ok = {"qid": "Q751917", "status": "ok", "fetcher_version": "1.5.2", "word_count": 2900,
          "via": "season_articles", "via_detail": "episode_table",
          "candidate": {"media_type": "tv_series"},
          "episode_tables": {"evidence": [{"title": "List of Midsomer Murders episodes",
                                           "basis": "main_article_link"}]}}
    _write(paths, "Q751917", ok)
    assert needs_fetch(paths, "Q751917", False)
    for older in ("1.5.3", "1.5.5"):  # 1.5.6: the back-link must be in the lead
        _write(paths, "Q751917", {**ok, "fetcher_version": older})
        assert needs_fetch(paths, "Q751917", False), older
    _write(paths, "Q751917", {**ok, "fetcher_version": FETCHER_VERSION})
    assert not needs_fetch(paths, "Q751917", False)
    # 1.5.5 episode-table files (its table_heading read years for every series)
    plain_tables = {"qid": "Q5", "status": "ok", "fetcher_version": "1.5.5",
                    "candidate": {"media_type": "tv_series"},
                    "episode_tables": {"evidence": [{"property": "P179"}]}}
    _write(paths, "Q5", plain_tables)
    assert needs_fetch(paths, "Q5", False)
    from laminary_pipeline.ingest.plots import _episode_tables_from_1_5_5 as from_1_5_5
    assert not from_1_5_5({**plain_tables, "fetcher_version": "1.5.4"})  # (1.5.8 re-fetches)
    # QA nit: run-rule titles too (Doctor Who's rule is older than the 1.5.6 fix)
    from laminary_pipeline.ingest.plots import _episode_tables_from_1_5_5

    assert _episode_tables_from_1_5_5({**plain_tables, "qid": "Q34316"})
    assert not _episode_tables_from_1_5_5({**plain_tables, "qid": "Q34316",
                                           "fetcher_version": "1.5.6"})
    other = {"qid": "Q1", "status": "skipped", "skip_reason": "too_short",
             "fetcher_version": "1.5.2", "candidate": {"media_type": "tv_series"},
             "season_articles": {"used": [], "skipped": [
                 {"title": "List of X episodes",
                  "reason": "unverified: Wikidata places item Q2 in another series (P361 Q3), "
                            "not Q1"}]}}
    _write(paths, "Q1", other)
    assert needs_fetch(paths, "Q1", False)
    # a 1.5.2 file verified by Wikidata alone is not fetched again
    from laminary_pipeline.ingest.plots import _link_fallback_before_hardening

    plain = {**ok, "qid": "Q3", "episode_tables": {"evidence": [{"property": "P179"}]}}
    assert not _link_fallback_before_hardening(plain)  # (1.5.7 re-fetches it for markers)
