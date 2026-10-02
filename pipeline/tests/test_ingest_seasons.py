"""Per-season articles for thin series (DECISIONS 2026-10-01), against synthetic responses."""

from __future__ import annotations

import copy
import csv
import hashlib
import io
from pathlib import Path
from typing import Any

import pytest
from wikimedia_fake import FakeWikimedia

from laminary_pipeline.annotate.config import DEFAULT_PROMPT_VERSION
from laminary_pipeline.annotate.cost import request_token_split, worst_case_request_usd
from laminary_pipeline.annotate.inputs import GateError, gate, parse_plot
from laminary_pipeline.annotate.prompt import (
    build_request,
    labeler_text,
    load_prompt,
    source_block_indices,
    verify_request,
)
from laminary_pipeline.annotation import validate_record
from laminary_pipeline.evaluate.report import evaluate
from laminary_pipeline.gold import columns as col
from laminary_pipeline.gold.importer import import_csv_text
from laminary_pipeline.gold.template import manifest_rows, template_csv, template_rows
from laminary_pipeline.ingest.http import HttpClient
from laminary_pipeline.ingest.plots import format_report, plots_report
from laminary_pipeline.ingest.seasons import guessed_titles, season_number, series_base
from laminary_pipeline.ingest.text import sha256_text, word_count
from laminary_pipeline.ingest.wikipedia import PlotFetcher

NOW = "2026-10-01T12:00:00Z"
SERIES = "Q9100000"
MAIN = "Tidewater (TV series)"


def words(n: int, tag: str) -> str:
    """n distinct words, so each season's text is recognizable."""
    return " ".join(f"{tag}{i}" for i in range(n)) + "."


def section_html(heading: str, body: str, *, table: bool = False) -> dict[str, Any]:
    inner = "<table><tr><td>Ep 1</td><td>An episode summary</td></tr></table>" if table else ""
    html = (
        f'<div class="mw-parser-output"><div class="mw-heading"><h2>{heading}</h2></div>'
        f"{inner}{f'<p>{body}</p>' if body else ''}</div>"
    )
    return {"parse": {"text": html}}


def page(title: str, pageid: int, revid: int, qid: str | None, *, redirect_from: str = "",
         ts: str = "2025-01-01T00:00:00Z") -> dict[str, Any]:
    p: dict[str, Any] = {"pageid": pageid, "ns": 0, "title": title,
                         "revisions": [{"revid": revid, "timestamp": ts}]}
    if qid:
        p["pageprops"] = {"wikibase_item": qid}
    q: dict[str, Any] = {"pages": [p]}
    if redirect_from:
        q["redirects"] = [{"from": redirect_from, "to": title}]
    return {"batchcomplete": True, "query": q}


def sections(title: str, headings: list[str]) -> dict[str, Any]:
    return {"parse": {"sections": [
        {"toclevel": 1, "level": "2", "line": h, "index": str(i + 1),
         "fromtitle": title.replace(" ", "_")}
        for i, h in enumerate(headings)
    ]}}


SEASON_WORDS = {1: 510, 2: 520, 4: 2900}


def season_fake(
    *, ordinals: dict[str, int] | None = None, season_words: dict[int, int] | None = None
) -> FakeWikimedia:
    """A thin main article and its seasons:

    - season 1 ("Tidewater season 1", also reached via the redirect "Tidewater (season 1)"):
      Plot, 510 words (just over the 500-word stub threshold, so not a stub).
    - season 2 ("Tidewater (season 2)", found only through a link in the main article):
      an Episodes table, a table-only Season overview, then Synopsis with 520 words.
    - season 3: Wikidata doesn't place it in the series, so it is unverifiable.
    - season 4: verified, 2,900 words: crosses the 3,000-word cap (1,030 + 2,900).
    - "List of Tidewater episodes": verified (P361), but unused while seasons have text.
    - "Tidewater season 6" redirects back to the main article.
    """
    fake = FakeWikimedia()
    w = fake.data["wikipedia"]
    w[f"query:{MAIN}"] = page(MAIN, 9100, 91000, SERIES)
    w["sections:91000"] = sections(MAIN, ["Premise", "Cast"])
    w["text:91000:1"] = section_html("Premise", words(40, "premise"))
    w["links:91000"] = {"parse": {"links": [
        {"ns": 0, "title": "Tidewater (season 2)", "exists": True},
        {"ns": 0, "title": "Harbor Lights", "exists": True},
        {"ns": 2, "title": "User:Tidewater season 9", "exists": True},
    ]}}
    pages = {
        1: ("Tidewater season 1", 9101, 91010, "Q9100001", ["Plot", "Production"]),
        2: ("Tidewater (season 2)", 9102, 91020, "Q9100002",
            ["Episodes", "Season overview", "Synopsis"]),
        3: ("Tidewater season 3", 9103, 91030, "Q9100003", ["Plot"]),
        4: ("Tidewater season 4", 9104, 91040, "Q9100004", ["Plot"]),
    }
    for n, (title, pid, rev, qid, heads) in pages.items():
        w[f"query:{title}"] = page(title, pid, rev, qid)
        w[f"sections:{rev}"] = sections(title, heads)
        for i, h in enumerate(heads, 1):
            if h in ("Plot", "Synopsis"):
                body = words({**SEASON_WORDS, **(season_words or {})}.get(n, 100), f"s{n}w")
                w[f"text:{rev}:{i}"] = section_html(h, body)
            else:
                w[f"text:{rev}:{i}"] = section_html(h, "", table=True)
    w["query:Tidewater (season 1)"] = page("Tidewater season 1", 9101, 91010, "Q9100001",
                                           redirect_from="Tidewater (season 1)")
    w["query:Tidewater season 6"] = page(MAIN, 9100, 91000, SERIES,
                                         redirect_from="Tidewater season 6")
    w["query:List of Tidewater episodes"] = page("List of Tidewater episodes", 9110, 91100,
                                                 "Q9100010")
    w["sections:91100"] = sections("List of Tidewater episodes", ["Series overview"])
    w["text:91100:1"] = section_html("Series overview", words(300, "list"))
    ords = {"Q9100001": 1, "Q9100002": 2, "Q9100004": 4, **(ordinals or {})}
    bindings = [
        {"item": {"value": f"http://www.wikidata.org/entity/{q}"},
         **({"ordinal": {"value": str(o)}} if o else {})}
        for q, o in ords.items()
    ] + [{"item": {"value": "http://www.wikidata.org/entity/Q9100010"}}]
    fake.data["sparql"]["season_check:" + SERIES] = {"results": {"bindings": bindings}}
    return fake


def fetch(fake: FakeWikimedia, **kw: Any) -> dict[str, Any]:
    client = HttpClient(fake, None, sleep=lambda s: None, min_interval={})
    cand = {"qid": SERIES, "enwiki_title": MAIN, "media_type": "tv_series", "title": "Tidewater",
            "year": 2019, "tmdb_id": 91000, "series_status": "ended",
            "series_status_basis": "P582"}
    return PlotFetcher(client, clock=lambda: NOW, **kw).fetch(cand)


# --- discovery helpers -----------------------------------------------------------------------


def test_title_helpers() -> None:
    assert series_base("Succession (TV series)") == "Succession"
    assert series_base("Fleabag") == "Fleabag"
    assert season_number("Succession season 2", "Succession") == 2
    assert season_number("Succession (season 3)", "Succession") == 3
    assert season_number("The Office (American season 4)", "The Office") == 4
    assert season_number("Fleabag series 1", "Fleabag") == 1
    assert season_number("Other show season 2", "Succession") is None
    assert season_number("Succession", "Succession") is None
    titles = guessed_titles("Tidewater", MAIN, max_seasons=2)
    assert titles[:4] == ["Tidewater season 1", "Tidewater (season 1)", "Tidewater series 1",
                          "Tidewater (series 1)"]
    assert titles[-2:] == ["List of Tidewater episodes",
                           "List of Tidewater (TV series) episodes"]


# --- joined text, order, cap, verification -------------------------------------------------


def test_thin_series_uses_verified_season_articles_in_order() -> None:
    rec = fetch(season_fake())
    assert rec["status"] == "ok" and rec["via"] == "season_articles"
    assert rec["main_article"]["skip_reason"] == "too_short"
    parts = rec["sources"]
    assert [p["page_title"] for p in parts] == ["Tidewater season 1", "Tidewater (season 2)"]
    assert [p["source"]["season"] for p in parts] == [1, 2]
    assert parts[0]["text"].startswith("s1w0 ") and parts[1]["text"].startswith("s2w0 ")
    assert "Ep 1" not in parts[1]["text"]  # episode table dropped
    assert parts[1]["sections"] == ["synopsis"]  # the table-only overview is not prose
    assert rec["word_count"] == 1030 == sum(p["source"]["word_count"] for p in parts)
    for p in parts:
        src = p["source"]
        assert src["content_sha256"] == sha256_text(p["text"])
        assert src["word_count"] == word_count(p["text"])
        assert src["ref"].startswith("https://en.wikipedia.org/wiki/Tidewater")
        assert "oldid=" in p["permalink"]
    report = rec["season_articles"]
    assert report["left_out_over_cap"] == ["Tidewater season 4"]
    reasons = {s["title"]: s["reason"] for s in report["skipped"]}
    assert reasons["Tidewater season 3"].startswith("unverified")
    assert not report["used_list_page"]


def test_cap_stops_at_a_season_boundary() -> None:
    """With a 600-word cap, season 2 (520 words) would cross it after season 1 (510): it is
    left out whole, not cut."""
    rec = fetch(season_fake(), season_word_cap=600)
    assert rec["status"] == "ok" and rec["word_count"] == 510
    assert rec["season_articles"]["used"] == ["Tidewater season 1"]
    assert rec["season_articles"]["left_out_over_cap"] == [
        "Tidewater (season 2)", "Tidewater season 4"
    ]


def _no_season_four(fake: FakeWikimedia) -> FakeWikimedia:
    """Season 4 is skipped (its ordinal disagrees), leaving seasons 1 and 2 only."""
    check = fake.data["sparql"]["season_check:" + SERIES]["results"]["bindings"]
    for b in check:
        if b["item"]["value"].endswith("Q9100004"):
            b["ordinal"] = {"value": "9"}
    return fake


def test_joined_text_under_the_minimum_is_too_short() -> None:
    """No full season (DECISIONS 2026-10-01, rule 5): stubs join as usual and the 150-word
    rule applies to the joined text, 60 + 70 words here."""
    rec = fetch(_no_season_four(season_fake(season_words={1: 60, 2: 70})))
    assert rec["status"] == "skipped" and rec["skip_reason"] == "too_short"
    assert rec["season_articles"]["used"] == ["Tidewater season 1", "Tidewater (season 2)"]
    assert "2 usable with 130 words" in rec["skip_detail"]


def test_stubs_without_a_full_season_still_join_under_the_cap() -> None:
    """Stubs only: the cap applies as in normal joining (120 + 130 crosses a 200-word cap)."""
    rec = fetch(_no_season_four(season_fake(season_words={1: 120, 2: 130})),
                season_word_cap=200)
    assert rec["status"] == "skipped" and rec["skip_reason"] == "too_short"
    assert rec["season_articles"]["used"] == ["Tidewater season 1"]
    assert rec["season_articles"]["left_out_over_cap"] == ["Tidewater (season 2)"]


def test_ordinal_that_disagrees_with_the_title_skips_the_page() -> None:
    rec = fetch(season_fake(ordinals={"Q9100002": 5}, season_words={1: 120}))
    reasons = {s["title"]: s["reason"] for s in rec["season_articles"]["skipped"]}
    assert "disagrees" in reasons["Tidewater (season 2)"]
    # season 1 (120 words) is a stub, so it leads season 4, the first full season: 3,020
    # words, over the cap but under the ceiling (DECISIONS 2026-10-01, lead block)
    assert rec["status"] == "ok" and rec["word_count"] == 3020
    assert [p["page_title"] for p in rec["sources"]] == [
        "Tidewater season 1", "Tidewater season 4"
    ]


def test_list_page_is_used_only_without_season_text() -> None:
    fake = season_fake()
    fake.data["sparql"]["season_check:" + SERIES] = {"results": {"bindings": [
        {"item": {"value": "http://www.wikidata.org/entity/Q9100010"}}
    ]}}
    rec = fetch(fake)
    assert rec["status"] == "ok" and rec["season_articles"]["used_list_page"]
    assert [p["page_title"] for p in rec["sources"]] == ["List of Tidewater episodes"]
    assert "season" not in rec["sources"][0]["source"]


def test_films_and_passing_series_never_look_for_seasons() -> None:
    fake = FakeWikimedia()
    client = HttpClient(fake, None, sleep=lambda s: None, min_interval={})
    fetcher = PlotFetcher(client, clock=lambda: NOW)
    film = fetcher.fetch({"qid": "Q9000002", "enwiki_title": "Nizhal Veedu",
                          "media_type": "movie"})
    assert film["skip_reason"] == "too_short" and "season_articles" not in film
    tv = fetcher.fetch({"qid": "Q9000003", "enwiki_title": "Harbor Lights (TV series)",
                        "media_type": "tv_series"})
    assert tv["status"] == "ok" and "via" not in tv
    assert not any("links" in r.url or r.host == "query.wikidata.org" for r in fake.requests)


def test_season_lookup_can_be_turned_off(tmp_path: Path) -> None:
    from laminary_pipeline.ingest.paths import DataPaths, write_json_atomic
    from laminary_pipeline.ingest.plots import needs_fetch

    fake = season_fake()
    rec = fetch(fake, season_articles=False)
    assert rec["status"] == "skipped" and rec["season_articles"] == {"status": "disabled"}
    assert not any(r.host == "query.wikidata.org" for r in fake.requests)
    # QA nit 5: marked, so later plots runs don't re-fetch it every time
    paths = DataPaths.resolve(str(tmp_path))
    write_json_atomic(paths.plot_file(SERIES), rec)
    assert not needs_fetch(paths, SERIES, False) and needs_fetch(paths, SERIES, True)


def test_season_pages_get_the_qid_check_and_stay_on_enwiki() -> None:
    fake = season_fake()
    fetch(fake)
    assert fake.hosts <= {"en.wikipedia.org", "query.wikidata.org"}
    checks = [r for r in fake.requests if r.host == "query.wikidata.org"]
    assert len(checks) == 1  # one verification query for all candidate pages


# --- annotation input: multi-source gate and tampering -------------------------------------


@pytest.fixture
def season_plot() -> dict[str, Any]:
    rec = fetch(season_fake())
    rec["candidate"].update(tmdb_id=91000, year=2019)
    return rec


def test_season_record_is_a_valid_multi_source_input(season_plot) -> None:
    plot = parse_plot(season_plot, "t")
    assert len(plot.sources) == 2 and plot.title["series_status"] == "ended"
    gated, params = build_request(plot, model="claude-opus-5-5",
                                  prompt=load_prompt(DEFAULT_PROMPT_VERSION),
                                  effort="medium", max_tokens=16000)
    content = params["messages"][0]["content"]
    assert content[0]["text"].endswith("The plot summary follows in 2 parts.")
    assert 'article="Tidewater season 1"' in content[1]["text"]
    assert 'part="2"' in content[4]["text"]
    assert 'article="Tidewater (season 2)"' in content[4]["text"]
    assert [content[i]["text"] for i in source_block_indices(2)] == [
        p["text"] for p in season_plot["sources"]
    ]


@pytest.mark.parametrize("change", ["text", "swap", "marker", "drop"])
def test_multi_source_tampering_is_caught(season_plot, change: str) -> None:
    prompt = load_prompt(DEFAULT_PROMPT_VERSION)
    gated, params = build_request(parse_plot(season_plot, "t"), model="claude-opus-5-5",
                                  prompt=prompt, effort="medium", max_tokens=16000)
    bad = copy.deepcopy(params)
    blocks = bad["messages"][0]["content"]
    if change == "text":
        blocks[5]["text"] = blocks[5]["text"].replace("s2w7", "s2w8")
    elif change == "swap":
        blocks[2], blocks[5] = blocks[5], blocks[2]
    elif change == "marker":
        blocks[4]["text"] = blocks[4]["text"].replace("season 2", "season 3")
    else:
        del blocks[4:7]
    with pytest.raises(GateError):
        verify_request(bad, gated, prompt)


def test_one_bad_season_source_rejects_the_title(season_plot) -> None:
    bad = copy.deepcopy(season_plot)
    bad["sources"][1]["text"] += " Extra."
    with pytest.raises(GateError, match="sha256"):
        gate(parse_plot(bad, "t"))
    bad = copy.deepcopy(season_plot)
    bad["sources"][0]["source"]["ref"] = "https://en.wikipedia.org/wiki/Talk:Tidewater"
    with pytest.raises(GateError, match="not articles"):
        gate(parse_plot(bad, "t"))


def test_worst_case_counts_every_season_block(season_plot) -> None:
    """Cost re-check: the bound covers all sources and markers, up to the 6,000-word
    ceiling."""
    prompt = load_prompt(DEFAULT_PROMPT_VERSION)
    _, params = build_request(parse_plot(season_plot, "t"), model="claude-opus-5-5",
                              prompt=prompt, effort="medium", max_tokens=16000)
    _, variable = request_token_split(params)
    one = copy.deepcopy(params)
    del one["messages"][0]["content"][4:7]
    _, variable_one = request_token_split(one)
    assert variable > variable_one  # season 2's block and markers are counted
    big = copy.deepcopy(params)
    for i in source_block_indices(2):
        big["messages"][0]["content"][i]["text"] = words(1500, "x")
    _, variable_big = request_token_split(big)
    assert variable_big > 3000 * 6 / 3.5  # 3,000 words of summary at the estimate's ratio
    usd = worst_case_request_usd("claude-opus-5-5", static_tokens=0,
                                 variable_tokens=variable_big, max_tokens=16000, batch=True)
    assert usd > 0
    # a two-source lead block at the 6,000-word ceiling (DECISIONS 2026-10-01) is priced from
    # the built request too: every word of both blocks counts
    lead = copy.deepcopy(params)
    for i, n in zip(source_block_indices(2), (100, 5900), strict=True):
        lead["messages"][0]["content"][i]["text"] = words(n, "x")
    _, variable_lead = request_token_split(lead)
    assert variable_lead > 6000 * 6 / 3.5 and variable_lead > variable_big
    usd_lead = worst_case_request_usd("claude-opus-5-5", static_tokens=0,
                                      variable_tokens=variable_lead, max_tokens=16000, batch=True)
    assert usd_lead > usd


# --- gold: labelers read exactly what the model reads ---------------------------------------


def test_gold_text_equals_the_joined_model_input(season_plot, tmp_path: Path) -> None:
    prompt = load_prompt(DEFAULT_PROMPT_VERSION)
    gated, params = build_request(parse_plot(season_plot, "t"), model="claude-opus-5-5",
                                  prompt=prompt, effort="medium", max_tokens=16000)
    blocks = [b["text"] for b in params["messages"][0]["content"]]
    joined = "\n\n".join("".join(blocks[1 + 3 * i: 4 + 3 * i]) for i in range(2))
    assert labeler_text(gated) == joined

    selection = [{"qid": SERIES, "title": "Tidewater", "year": 2019, "media_type": "tv_series",
                  "tmdb_id": 91000}]
    rows, skipped = template_rows(selection, {SERIES: season_plot})
    assert skipped == [] and rows[0]["plot_section"] == "Season articles (2)"
    assert rows[0]["source_sha256"] == " | ".join(
        p["source"]["content_sha256"] for p in season_plot["sources"]
    )
    manifest = manifest_rows(rows, {SERIES: season_plot})
    assert manifest[0]["sha256"] == hashlib.sha256(joined.encode("utf-8")).hexdigest()

    parsed = list(csv.DictReader(io.StringIO(template_csv(rows))))[1:]
    labels = {
        "labeler_id": "L01", "primary_plot": "Mystery",
        **{f"plot_{k}": ("Y" if k == "mystery" else "N") for k in col.BOOKER_PLOTS},
        "blueprint": "No clear blueprint", **{c: "N" for c in col.STAGE_COLS},
        "arc_shape": "Icarus (rise then fall)", **{c: "N" for c in col.TAG_COLS},
    }
    parsed[0].update(labels)
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(col.ALL_COLUMNS)
    w.writerow([parsed[0].get(c, "") for c in col.ALL_COLUMNS])
    result = import_csv_text(out.getvalue(), annotated_at=NOW)
    assert result.ok, "\n".join(map(str, result.errors))
    gold = result.records[0]
    assert validate_record(gold) == []
    assert [s["content_sha256"] for s in gold["provenance"]["sources"]] == [
        p["source"]["content_sha256"] for p in season_plot["sources"]
    ]
    assert gold["provenance"]["input_word_count"] == 1030


def test_mismatched_source_counts_in_the_sheet_are_errors(season_plot) -> None:
    selection = [{"qid": SERIES, "title": "Tidewater", "year": 2019, "media_type": "tv_series",
                  "tmdb_id": 91000}]
    rows, _ = template_rows(selection, {SERIES: season_plot})
    row = {**rows[0], "labeler_id": "L01", "source_revision": "91010"}
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(col.ALL_COLUMNS)
    w.writerow([row.get(c, "") for c in col.ALL_COLUMNS])
    result = import_csv_text(out.getvalue(), annotated_at=NOW)
    assert "same number of values" in str(result.errors[0])


def test_evaluation_matches_multi_source_records_by_all_hashes(season_plot) -> None:
    """A gold label made from the same season articles is scored, not excluded."""
    from test_evaluate import model_record

    from laminary_pipeline.annotate.inputs import ingest_sources

    srcs = [{k: v for k, v in s.items() if k != "text"} for s in ingest_sources(season_plot)]
    rec = model_record(91000, "the_matrix")
    rec["provenance"]["sources"] = srcs
    rec["provenance"]["input_word_count"] = 1030
    assert validate_record(rec) == []
    gold = copy.deepcopy(rec)
    gold["record_kind"] = "gold_label"
    gold["provenance"] = {**gold["provenance"], "annotator": {"labeler_id": "L01",
                                                              "guide_version": "1.2.0"}}
    del gold["provenance"]["usage"]
    gold["layers"] = {
        "archetypal_plot": {"primary": {"label": "overcoming_the_monster"},
                            "plots": rec["layers"]["archetypal_plot"]["plots"]},
        "mythic_blueprint": rec["layers"]["mythic_blueprint"],
        "structural_skeleton": {"emotional_arc": rec["layers"]["structural_skeleton"][
            "emotional_arc"]},
    }
    report = evaluate([gold], [rec])
    assert report["counts"]["source_hash_mismatches"] == []
    assert report["headline"]["total"] == 1


# --- report ----------------------------------------------------------------------------------


def test_report_counts_titles_via_season_articles(tmp_path: Path) -> None:
    from laminary_pipeline.ingest.paths import DataPaths, write_json_atomic

    paths = DataPaths.resolve(str(tmp_path))
    rec = fetch(season_fake())
    write_json_atomic(paths.plot_file(SERIES), rec)
    cands = [{"qid": SERIES, "media_type": "tv_series", "region": "english",
              "language": "english", "decade": "2010s", "bucket": "tv:english", "role": "pilot",
              "bucket_rank": 1}]
    report = plots_report(paths, cands)
    assert report["via_season_articles"] == {"total": 1, "by_bucket": {"tv:english": 1}}
    assert "Via season articles: 1" in format_report(report)
    # seasons 1-2 of 4 verified pages: partial; nothing dropped in the synthetic sections
    assert report["non_plot_filter"] == {"titles_with_parts_dropped": 0,
                                         "titles_with_a_section_rejected": 0,
                                         "partial_season_coverage": 1}
    assert "1 passing series cover only some seasons" in format_report(report)


def test_thin_series_from_an_older_fetcher_are_fetched_again(tmp_path: Path) -> None:
    from laminary_pipeline.ingest.paths import DataPaths, write_json_atomic
    from laminary_pipeline.ingest.plots import needs_fetch

    paths = DataPaths.resolve(str(tmp_path))
    old = {"qid": "Q1", "status": "skipped", "skip_reason": "too_short",
           "candidate": {"media_type": "tv_series"}}
    write_json_atomic(paths.plot_file("Q1"), old)
    assert needs_fetch(paths, "Q1", False)
    write_json_atomic(paths.plot_file("Q1"), {**old, "season_articles": {"used": []}})
    assert not needs_fetch(paths, "Q1", False)  # already tried with the fallback
    write_json_atomic(paths.plot_file("Q1"), {**old, "candidate": {"media_type": "movie"}})
    assert not needs_fetch(paths, "Q1", False)


# --- lead block (DECISIONS 2026-10-01) -----------------------------------------------------


def test_season_one_over_the_cap_is_used_alone_and_whole() -> None:
    rec = fetch(season_fake(season_words={1: 3500}))
    assert rec["status"] == "ok"
    assert [p["page_title"] for p in rec["sources"]] == ["Tidewater season 1"]
    assert rec["word_count"] == 3500 == word_count(rec["sources"][0]["text"])  # not cut
    assert rec["season_articles"]["left_out_over_cap"] == [
        "Tidewater (season 2)", "Tidewater season 4"
    ]


def test_a_later_season_over_the_cap_is_excluded() -> None:
    rec = fetch(season_fake(season_words={1: 600, 2: 3200}))  # season 1 full (500+ words)
    assert rec["status"] == "ok"
    assert [p["page_title"] for p in rec["sources"]] == ["Tidewater season 1"]
    assert rec["word_count"] == 600
    assert rec["season_articles"]["left_out_over_cap"][0] == "Tidewater (season 2)"


def test_season_one_over_the_ceiling_skips_the_title() -> None:
    from laminary_pipeline.ingest.seasons import SEASON_ONE_CEILING

    assert SEASON_ONE_CEILING == 6000
    rec = fetch(season_fake(season_words={1: 6001}))
    assert rec["status"] == "skipped" and rec["skip_reason"] == "season_too_long"
    assert "Tidewater season 1 (6001 words)" in rec["skip_detail"]
    assert not rec["season_articles"]["used_list_page"]  # no fallback to the list page
    # no stubs before it, so no stubs-only fallback either (DECISIONS 2026-10-01)
    assert rec["season_articles"]["used"] == []
    assert rec["season_articles"]["left_out_over_cap"] == [
        "Tidewater season 1", "Tidewater (season 2)", "Tidewater season 4"
    ]
    ok = fetch(season_fake(season_words={1: 6000}))
    assert ok["status"] == "ok" and ok["word_count"] == 6000


# --- gate limits on multi-source input (QA should-fix 1, probe7) ---------------------------


def _plot_with(texts: list[str], *, seasons: bool = True, via: str | None = None):
    from annotate_support import source_meta

    from laminary_pipeline.annotate.inputs import PlotInput, PlotSource

    srcs = []
    for i, text in enumerate(texts, 1):
        meta = source_meta(text, ref=f"https://en.wikipedia.org/wiki/Tidewater_season_{i}")
        if seasons:
            meta["season"] = i
        srcs.append(PlotSource(meta, text))
    return PlotInput({"media_type": "tv_series", "tmdb_id": 91000}, tuple(srcs), "t", via=via)


def test_gate_refuses_more_sources_than_a_record_holds() -> None:
    from laminary_pipeline.annotate.inputs import max_sources

    assert max_sources() == 20
    with pytest.raises(GateError, match="21 sources, a record holds at most 20"):
        gate(_plot_with([words(10, f"p{i}w") for i in range(21)]))
    assert len(gate(_plot_with([words(10, f"p{i}w") for i in range(20)])).plot.sources) == 20


def test_gate_bounds_multi_source_input_by_the_ceiling() -> None:
    """A lead block (stub seasons + the first full season) may be several sources up to the
    6,000-word ceiling (DECISIONS 2026-10-01)."""
    gate(_plot_with([words(100, "a"), words(5900, "b")]))  # exactly at the ceiling
    with pytest.raises(GateError, match="2 source\\(s\\) and 6001 words, over the 6000-word"):
        gate(_plot_with([words(100, "a"), words(5901, "b")]))
    with pytest.raises(GateError, match="over the 6000-word ceiling"):  # even without seasons
        gate(_plot_with([words(3000, "a"), words(3001, "b")], seasons=False))


def test_gate_bounds_a_lone_season_by_the_ceiling() -> None:
    gate(_plot_with([words(6000, "a")]))  # a lead block of one full season
    with pytest.raises(GateError, match="over the 6000-word ceiling"):
        gate(_plot_with([words(6001, "a")]))
    gate(_plot_with([words(6001, "a")], seasons=False))  # a main article has no upper limit


# --- QA nits 2 and 3 -------------------------------------------------------------------------


def test_malformed_wikidata_items_are_skipped_not_interpolated() -> None:
    from laminary_pipeline.ingest.seasons import season_check_sparql

    fake = season_fake()
    for key in ("query:Tidewater season 1", "query:Tidewater (season 1)"):  # page + redirect
        q = fake.data["wikipedia"][key]["query"]["pages"][0]
        q["pageprops"]["wikibase_item"] = "Q1 } UNION { ?x ?y ?z"
    rec = fetch(fake)
    reasons = {s["title"]: s["reason"] for s in rec["season_articles"]["skipped"]}
    assert reasons["Tidewater season 1"].startswith("malformed Wikidata item")
    query = next(r for r in fake.requests if r.host == "query.wikidata.org").data.decode()
    assert "UNION+%7B+%3Fx" not in query
    with pytest.raises(ValueError, match="not Wikidata item ids"):
        season_check_sparql(SERIES, ["Q1 } UNION {"])


def _season_one_unusable(fake: FakeWikimedia, how: str) -> None:
    w = fake.data["wikipedia"]
    if how == "no text":
        w["sections:91010"] = sections("Tidewater season 1", ["Production"])
    elif how == "unverified":
        check = fake.data["sparql"]["season_check:" + SERIES]["results"]["bindings"]
        check[:] = [b for b in check if not b["item"]["value"].endswith("Q9100001")]
    else:  # missing
        for key in ("query:Tidewater season 1", "query:Tidewater (season 1)"):
            del w[key]


@pytest.mark.parametrize("how", ["no text", "unverified", "missing"])
def test_first_usable_season_over_the_cap_is_used_alone(how: str) -> None:
    """DECISIONS 2026-10-01: the lead block starts at the first available season."""
    fake = season_fake(season_words={2: 3500})
    _season_one_unusable(fake, how)
    rec = fetch(fake)
    assert rec["status"] == "ok" and not rec["season_articles"]["used_list_page"]
    assert [p["page_title"] for p in rec["sources"]] == ["Tidewater (season 2)"]
    assert rec["word_count"] == 3500 == word_count(rec["sources"][0]["text"])
    assert rec["season_articles"]["left_out_over_cap"] == ["Tidewater season 4"]


@pytest.mark.parametrize("how", ["no text", "unverified", "missing"])
def test_first_usable_season_over_the_ceiling_skips_the_title(how: str) -> None:
    fake = season_fake(season_words={2: 6001})
    _season_one_unusable(fake, how)
    rec = fetch(fake)
    assert rec["status"] == "skipped" and rec["skip_reason"] == "season_too_long"
    assert "Tidewater (season 2) (6001 words)" in rec["skip_detail"]
    assert not rec["season_articles"]["used_list_page"]


def test_usable_season_one_under_the_cap_behaves_as_before() -> None:
    """Season 1 is a full season (500+ words) under the cap, so season 2 over the cap is left
    out, not used alone."""
    rec = fetch(season_fake(season_words={1: 600, 2: 3500}))
    assert [p["page_title"] for p in rec["sources"]] == ["Tidewater season 1"]
    assert rec["season_articles"]["left_out_over_cap"][0] == "Tidewater (season 2)"


# --- stub seasons lead the first full season (DECISIONS 2026-10-01) ------------------------


def _titles(rec: dict[str, Any]) -> list[str]:
    return [p["page_title"] for p in rec["sources"]]


S1, S2, S4 = "Tidewater season 1", "Tidewater (season 2)", "Tidewater season 4"


def test_stub_season_one_leads_a_long_season_two() -> None:
    rec = fetch(season_fake(season_words={1: 100, 2: 3500}))
    assert rec["status"] == "ok" and not rec["season_articles"]["used_list_page"]
    assert _titles(rec) == [S1, S2]
    assert rec["word_count"] == 3600 == sum(word_count(p["text"]) for p in rec["sources"])
    assert rec["season_articles"]["left_out_over_cap"] == [S4]
    gate(parse_plot(rec, "t"))  # a two-source lead block over the cap passes the gate


def test_two_stub_seasons_lead_the_first_full_season() -> None:
    """Hari's example: s1=120 + s2=130 + s3=2,900 (season 4 in this fixture) -> all three."""
    rec = fetch(season_fake(season_words={1: 120, 2: 130, 4: 2900}))
    assert rec["status"] == "ok"
    assert _titles(rec) == [S1, S2, S4]
    assert rec["word_count"] == 3150
    assert rec["season_articles"]["left_out_over_cap"] == []
    gate(parse_plot(rec, "t"))


def test_lead_block_is_bounded_by_the_ceiling() -> None:
    """100 + 5,900 is at the ceiling and used whole. 100 + 5,901 is over it: the full season
    is dropped and the 100-word stub alone fails the 150-word minimum (DECISIONS 2026-10-01,
    fallback; was season_too_long)."""
    ok = fetch(season_fake(season_words={1: 100, 2: 5900}))
    assert ok["status"] == "ok" and ok["word_count"] == 6000
    assert _titles(ok) == [S1, S2]
    gate(parse_plot(ok, "t"))
    rec = fetch(season_fake(season_words={1: 100, 2: 5901}))
    assert rec["status"] == "skipped" and rec["skip_reason"] == "too_short"
    assert "1 usable with 100 words" in rec["skip_detail"]
    assert not rec["season_articles"]["used_list_page"]  # no fallback to the list page
    assert rec["season_articles"]["used"] == [S1]
    assert rec["season_articles"]["left_out_over_cap"] == [S2, S4]


def test_short_lead_block_joins_later_seasons_under_the_cap() -> None:
    """The ">1000" in Hari's wording is not a threshold: a short first full season is plain
    joining under the cap."""
    rec = fetch(season_fake(season_words={1: 100, 2: 1000, 4: 1500}))
    assert rec["status"] == "ok" and _titles(rec) == [S1, S2, S4]
    assert rec["word_count"] == 2600
    rec = fetch(season_fake(season_words={1: 100, 2: 600}))
    assert _titles(rec) == [S1, S2] and rec["season_articles"]["left_out_over_cap"] == [S4]


def test_lead_block_over_the_cap_takes_nothing_more() -> None:
    rec = fetch(season_fake(season_words={1: 100, 2: 3500, 4: 500}))
    assert rec["status"] == "ok" and _titles(rec) == [S1, S2]
    assert rec["word_count"] == 3600
    assert rec["season_articles"]["left_out_over_cap"] == [S4]


def test_lead_block_within_the_cap_keeps_joining_later_seasons() -> None:
    """QA: a lead block under the cap is not a stopping point. 100 + 2,850 + 40 = 2,990 fits;
    100 + 2,950 = 3,050 is over the cap, so season 4 (40 words) is left out."""
    rec = fetch(season_fake(season_words={1: 100, 2: 2850, 4: 40}))
    assert rec["status"] == "ok" and _titles(rec) == [S1, S2, S4]
    assert rec["word_count"] == 2990 and rec["season_articles"]["left_out_over_cap"] == []
    rec = fetch(season_fake(season_words={1: 100, 2: 2950, 4: 40}))
    assert _titles(rec) == [S1, S2] and rec["word_count"] == 3050
    assert rec["season_articles"]["left_out_over_cap"] == [S4]


def test_lead_block_within_the_cap_stops_at_a_season_boundary() -> None:
    rec = fetch(season_fake(season_words={1: 100, 2: 1000, 4: 2000}))
    assert rec["status"] == "ok" and _titles(rec) == [S1, S2]
    assert rec["word_count"] == 1100
    assert rec["season_articles"]["left_out_over_cap"] == [S4]


@pytest.mark.parametrize(("s1", "used"), [(499, [S1, S2]), (500, [S1])])
def test_stub_boundary_is_500_words(s1: int, used: list[str]) -> None:
    """DECISIONS 2026-10-01: a stub is under 500 words; the 150-word annotation minimum is a
    separate, unchanged constant."""
    from laminary_pipeline.ingest.seasons import STUB_SEASON_WORDS
    from laminary_pipeline.ingest.wikipedia import MIN_WORDS

    assert STUB_SEASON_WORDS == 500 and MIN_WORDS == 150
    rec = fetch(season_fake(season_words={1: s1, 2: 3500}))
    assert rec["status"] == "ok" and _titles(rec) == used
    assert rec["season_articles"]["stub_season_words"] == 500
    if s1 == 500:  # a full season 1: season 2 can't join under the cap, so it is left out
        assert rec["season_articles"]["left_out_over_cap"] == [S2, S4]
    else:
        assert rec["word_count"] == 3999


def test_stub_season_one_of_300_words_leads_season_two() -> None:
    """Hari's case: a 300-word season 1 (over 150, under 500) no longer blocks a long season
    2."""
    rec = fetch(season_fake(season_words={1: 300, 2: 3500}))
    assert rec["status"] == "ok" and _titles(rec) == [S1, S2]
    assert rec["word_count"] == 3800 and rec["season_articles"]["left_out_over_cap"] == [S4]
    gate(parse_plot(rec, "t"))


def test_two_stubs_over_150_lead_the_first_full_season() -> None:
    """s1=300 + s2=400 + s3=3,000 (season 4 in this fixture) -> all three."""
    rec = fetch(season_fake(season_words={1: 300, 2: 400, 4: 3000}))
    assert rec["status"] == "ok" and _titles(rec) == [S1, S2, S4]
    assert rec["word_count"] == 3700
    gate(parse_plot(rec, "t"))


def test_stub_lead_block_one_word_over_the_ceiling_uses_the_stub_alone() -> None:
    """DECISIONS 2026-10-01 (fallback): 450 + 5,551 = 6,001 is over the ceiling, so season 2
    and later pages are left out and season 1 is used alone (was season_too_long)."""
    rec = fetch(season_fake(season_words={1: 450, 2: 5551}))
    assert rec["status"] == "ok" and _titles(rec) == [S1]
    assert rec["word_count"] == 450
    assert rec["season_articles"]["left_out_over_cap"] == [S2, S4]
    assert not rec["season_articles"]["used_list_page"]
    gate(parse_plot(rec, "t"))


def test_two_stubs_over_the_ceiling_are_used_without_the_full_season() -> None:
    """499 + 499 + 5,003 = 6,001: both stubs (998 words, under the cap) are used alone."""
    rec = fetch(season_fake(season_words={1: 499, 2: 499, 4: 5003}))
    assert rec["status"] == "ok" and _titles(rec) == [S1, S2]
    assert rec["word_count"] == 998 and rec["season_articles"]["left_out_over_cap"] == [S4]
    gate(parse_plot(rec, "t"))


def test_stubs_alone_under_the_minimum_are_too_short() -> None:
    """60 + 70 + 5,900 = 6,030 is over the ceiling; the stubs alone (130) are under 150."""
    rec = fetch(season_fake(season_words={1: 60, 2: 70, 4: 5900}))
    assert rec["status"] == "skipped" and rec["skip_reason"] == "too_short"
    assert "2 usable with 130 words" in rec["skip_detail"]
    assert rec["season_articles"]["used"] == [S1, S2]
    assert rec["season_articles"]["left_out_over_cap"] == [S4]
    assert not rec["season_articles"]["used_list_page"]


def test_stub_before_a_short_full_season_is_a_normal_join() -> None:
    """s1=200 (stub) + s2=600 (full) = 800, within the cap: later seasons join under the cap,
    so season 4 (2,900) is left out exactly as plain joining would leave it out."""
    rec = fetch(season_fake(season_words={1: 200, 2: 600}))
    assert rec["status"] == "ok" and _titles(rec) == [S1, S2]
    assert rec["word_count"] == 800 and rec["season_articles"]["left_out_over_cap"] == [S4]


def test_stubs_only_join_and_pass_the_150_word_minimum() -> None:
    """No season reaches 500 words: the stubs join under the cap as usual and the joined 600
    words pass the 150-word minimum."""
    rec = fetch(_no_season_four(season_fake(season_words={1: 300, 2: 300})))
    assert rec["status"] == "ok" and _titles(rec) == [S1, S2]
    assert rec["word_count"] == 600 and rec["season_articles"]["left_out_over_cap"] == []
    gate(parse_plot(rec, "t"))


def test_lead_block_of_exactly_the_cap_leaves_the_next_season_out() -> None:
    """QA: 100 + 2,900 = 3,000 is within the cap, so later seasons may join, but even a
    40-word season 4 would cross it."""
    rec = fetch(season_fake(season_words={1: 100, 2: 2900, 4: 40}))
    assert rec["status"] == "ok" and _titles(rec) == [S1, S2]
    assert rec["word_count"] == 3000 and rec["season_articles"]["left_out_over_cap"] == [S4]


def test_stub_after_a_missing_season_one_leads_the_next_full_season() -> None:
    fake = season_fake(season_words={2: 100, 4: 3500})
    _season_one_unusable(fake, "missing")
    rec = fetch(fake)
    assert rec["status"] == "ok" and _titles(rec) == [S2, S4]
    assert rec["word_count"] == 3600


def test_a_stub_after_a_full_season_joins_normally() -> None:
    """Stubs only lead; after the first full season, joining is the usual cap rule."""
    rec = fetch(season_fake(season_words={1: 2900, 2: 100, 4: 50}))
    assert _titles(rec) == [S1, S2] and rec["word_count"] == 3000
    assert rec["season_articles"]["left_out_over_cap"] == [S4]


def test_gate_caps_a_lone_list_page_from_season_articles() -> None:
    """QA nit: a single episode-list page has no season, but ingest joins it only under the
    ~3,000-word cap, so via season_articles it is bounded by the cap, not left unlimited."""
    gate(_plot_with([words(3000, "a")], seasons=False, via="season_articles"))
    with pytest.raises(GateError, match="3001 words, over the 3000-word episode-list page"):
        gate(_plot_with([words(3001, "a")], seasons=False, via="season_articles"))
    gate(_plot_with([words(6001, "a")], seasons=False))  # a main article: still no limit


def test_gate_bounds_any_season_article_input_by_the_ceiling() -> None:
    """via season_articles is enough to bound an input, even if a source lost its season."""
    gate(_plot_with([words(6000, "a")], via="season_articles"))
    with pytest.raises(GateError, match="over the 6000-word ceiling"):
        gate(_plot_with([words(6001, "a")], via="season_articles"))


def test_the_via_marker_reaches_the_gate_from_an_ingest_plot_file() -> None:
    fake = season_fake()
    fake.data["sparql"]["season_check:" + SERIES] = {"results": {"bindings": [
        {"item": {"value": "http://www.wikidata.org/entity/Q9100010"}}
    ]}}
    rec = fetch(fake)
    assert rec["season_articles"]["used_list_page"]
    plot = parse_plot(rec, "t")
    assert plot.via == "season_articles"
    gate(plot)
    big = copy.deepcopy(rec)  # a list page over the cap, hashes kept consistent
    text = words(3001, "list")
    big["sources"][0]["text"] = text
    big["sources"][0]["source"].update(word_count=3001, content_sha256=sha256_text(text))
    with pytest.raises(GateError, match="episode-list page"):
        gate(parse_plot(big, "t"))
    from laminary_pipeline.gold.template import labeler_text_for

    with pytest.raises(GateError, match="episode-list page"):  # gold texts use the same gate
        labeler_text_for(big)


# --- the source limit inside the lead block (QA) ---------------------------------------------


def many_seasons_fake(season_words: list[int]) -> FakeWikimedia:
    """A thin main article and len(season_words) verified seasons, "Tidewater season N" with
    a Plot section of the given length. Seasons past MAX_SEASONS are found through links."""
    fake = FakeWikimedia()
    w = fake.data["wikipedia"]
    w[f"query:{MAIN}"] = page(MAIN, 9100, 91000, SERIES)
    w["sections:91000"] = sections(MAIN, ["Premise", "Cast"])
    w["text:91000:1"] = section_html("Premise", words(40, "premise"))
    w["links:91000"] = {"parse": {"links": [
        {"ns": 0, "title": f"Tidewater season {n}", "exists": True}
        for n in range(1, len(season_words) + 1)
    ]}}
    bindings = []
    for n, count in enumerate(season_words, 1):
        title, rev, qid = f"Tidewater season {n}", 92000 + n, f"Q92000{n:02d}"
        w[f"query:{title}"] = page(title, 9200 + n, rev, qid)
        w[f"sections:{rev}"] = sections(title, ["Plot"])
        w[f"text:{rev}:1"] = section_html("Plot", words(count, f"s{n}w"))
        bindings.append({"item": {"value": f"http://www.wikidata.org/entity/{qid}"},
                         "ordinal": {"value": str(n)}})
    fake.data["sparql"]["season_check:" + SERIES] = {"results": {"bindings": bindings}}
    return fake


def test_source_limit_reached_inside_the_lead_block() -> None:
    """20 stubs fill MAX_SEASON_SOURCES before the first full season (season 21) is reached:
    season 21 is left out at the source limit and the stubs join under the cap as stubs-only
    (100 x 20 = 2,000 words, all kept)."""
    from laminary_pipeline.ingest.seasons import MAX_SEASON_SOURCES

    assert MAX_SEASON_SOURCES == 20
    rec = fetch(many_seasons_fake([100] * 20 + [3000]))
    assert rec["status"] == "ok" and len(rec["sources"]) == 20
    assert _titles(rec) == [f"Tidewater season {n}" for n in range(1, 21)]
    assert rec["word_count"] == 2000
    assert rec["season_articles"]["left_out_over_cap"] == ["Tidewater season 21"]
    gate(parse_plot(rec, "t"))


def test_source_limit_inside_the_lead_block_then_the_cap_applies() -> None:
    """20 stubs of 400 words (8,000) hit the source limit before season 21: as stubs only they
    join under the cap, 7 x 400 = 2,800, and the rest are left out in order."""
    rec = fetch(many_seasons_fake([400] * 20 + [3000]))
    assert rec["status"] == "ok" and rec["word_count"] == 2800
    assert _titles(rec) == [f"Tidewater season {n}" for n in range(1, 8)]
    assert rec["season_articles"]["left_out_over_cap"] == [
        f"Tidewater season {n}" for n in range(8, 22)
    ]


# --- stubs alone when the lead block is over the ceiling (DECISIONS 2026-10-01) --------------


def test_stubs_over_the_ceiling_are_trimmed_under_the_cap() -> None:
    """13 x 490 + 500 = 6,870: season 14 is dropped and the stubs-only trim keeps 6 stubs
    (2,940; a 7th would make 3,430). The rest are left out in order (was skipped)."""
    rec = fetch(many_seasons_fake([490] * 13 + [500]))
    assert rec["status"] == "ok" and rec["word_count"] == 2940
    assert _titles(rec) == [f"Tidewater season {n}" for n in range(1, 7)]
    assert rec["season_articles"]["left_out_over_cap"] == [
        f"Tidewater season {n}" for n in range(7, 15)
    ]
    gate(parse_plot(rec, "t"))


def test_ceiling_fallback_matches_the_source_limit_case() -> None:
    """QA's inconsistency: 19 x 499 + 500 (over the ceiling) and 20 x 499 + 3,000 (source
    limit first) now both end as stubs only, trimmed to the same 6 stubs (2,994 words)."""
    over = fetch(many_seasons_fake([499] * 19 + [500]))
    limit = fetch(many_seasons_fake([499] * 20 + [3000]))
    for rec, last in ((over, 20), (limit, 21)):
        assert rec["status"] == "ok" and rec["word_count"] == 2994
        assert _titles(rec) == [f"Tidewater season {n}" for n in range(1, 7)]
        assert rec["season_articles"]["left_out_over_cap"] == [
            f"Tidewater season {n}" for n in range(7, last + 1)
        ]
        gate(parse_plot(rec, "t"))


# --- Wikidata evidence, coverage, non-plot filter (fetcher 1.4.0, DECISIONS 2026-10-02) ------


def fetch_cand(fake: FakeWikimedia, **cand_extra: Any) -> dict[str, Any]:
    client = HttpClient(fake, None, sleep=lambda s: None, min_interval={})
    cand = {"qid": SERIES, "enwiki_title": MAIN, "media_type": "tv_series", "title": "Tidewater",
            "year": 2019, "tmdb_id": 91000, "series_status": "ended",
            "series_status_basis": "P582", **cand_extra}
    return PlotFetcher(client, clock=lambda: NOW).fetch(cand)


def test_used_season_pages_record_their_wikidata_statement() -> None:
    from laminary_pipeline.ingest.seasons import lead_block_ceiling

    fake = season_fake()
    for b in fake.data["sparql"]["season_check:" + SERIES]["results"]["bindings"]:
        if b["item"]["value"].endswith("Q9100002"):  # season 2 is only "part of" the series
            b.pop("ordinal", None)
            b["prop"] = {"value": "P361"}
    rec = fetch(fake)
    report = rec["season_articles"]
    assert report["evidence"] == [
        {"title": "Tidewater season 1", "item": "Q9100001", "property": "P179",
         "series": SERIES, "ordinal": 1},
        {"title": "Tidewater (season 2)", "item": "Q9100002", "property": "P361",
         "series": SERIES, "ordinal": None},
    ]
    assert report["lead_block_ceiling"] == 6000 and "season_one_ceiling" not in report
    assert lead_block_ceiling(report) == 6000
    assert lead_block_ceiling({"season_one_ceiling": 6000}) == 6000  # files before 1.4.0
    assert lead_block_ceiling({}) is None
    assert report["verified_seasons"] == [1, 2, 4]


def test_bindings_without_a_known_property_are_not_evidence() -> None:
    fake = season_fake()
    for b in fake.data["sparql"]["season_check:" + SERIES]["results"]["bindings"]:
        if b["item"]["value"].endswith("Q9100002"):
            b["prop"] = {"value": "P31"}
    rec = fetch(fake)
    reasons = {s["title"]: s["reason"] for s in rec["season_articles"]["skipped"]}
    assert reasons["Tidewater (season 2)"].startswith("unverified")


def test_choose_evidence_prefers_the_matching_series_ordinal() -> None:
    from laminary_pipeline.ingest.seasons import Evidence, choose_evidence

    p361 = Evidence("Q5", "P361", SERIES, None)
    bare = Evidence("Q5", "P179", SERIES, None)
    two = Evidence("Q5", "P179", SERIES, 2)
    three = Evidence("Q5", "P179", SERIES, 3)
    assert choose_evidence([p361, bare, three, two], 2) == two
    assert choose_evidence([p361, three], None) == three
    assert choose_evidence([p361, bare], 2) == bare
    assert choose_evidence([p361], 2) == p361
    assert choose_evidence([], 2) is None


def test_season_query_asks_which_property_matched() -> None:
    from laminary_pipeline.ingest.seasons import season_check_sparql

    q = season_check_sparql(SERIES, ["Q9100001"])
    assert 'BIND("P179" AS ?prop)' in q and 'BIND("P361" AS ?prop)' in q
    assert "SELECT ?item ?prop ?ordinal" in q and "GROUP BY" not in q


@pytest.mark.parametrize(
    ("extra", "total", "basis", "partial"),
    [
        ({}, 4, "verified_season_pages", True),  # pages 1, 2 and 4 verified; 1-2 used
        ({"number_of_seasons": 6}, 6, "wikidata_P2437", True),
        ({"number_of_seasons": 3}, 4, "verified_season_pages", True),  # under page 4: ignored
    ],
)
def test_coverage_of_a_season_article_text(
    extra: dict[str, Any], total: int, basis: str, partial: bool
) -> None:
    rec = fetch_cand(season_fake(), **extra)
    cov = rec["coverage"]
    assert cov["seasons"] == [1, 2]
    assert (cov["total_seasons"], cov["total_seasons_basis"], cov["partial"]) == (
        total, basis, partial)
    assert ("wikidata_number_of_seasons_ignored" in cov) == (extra.get("number_of_seasons") == 3)
    assert rec["candidate"]["number_of_seasons"] == extra.get("number_of_seasons")


def test_season_coverage_rules() -> None:
    from laminary_pipeline.ingest.seasons import season_coverage

    full = season_coverage([1, 2, 3], [1, 2, 3], 3)
    assert full == {"seasons": [1, 2, 3], "total_seasons": 3,
                    "total_seasons_basis": "wikidata_P2437", "partial": False}
    assert season_coverage([3, 4, 5, 6], [3, 4, 5, 6], None)["partial"] is True  # iCarly-like
    assert season_coverage([None], [], 5) is None  # an episode-list page: no season numbers
    assert season_coverage([], [], 5) is None
    bad = season_coverage([1, 2], [1, 2], True)  # not a count
    assert bad is not None and bad["total_seasons_basis"] == "verified_season_pages"
    assert bad["total_seasons"] == 2 and bad["partial"] is False


def test_wikidata_season_count_parsing() -> None:
    from laminary_pipeline.ingest.wikidata import season_count

    assert season_count("7") == 7 and season_count("7.0") == 7
    for value in (None, "7.5", "0", "-1", "101", "seven"):
        assert season_count(value) is None


def test_main_article_episodes_section_of_production_text_falls_back_to_seasons() -> None:
    """QA's blocker on synthetic markup: an 'Episodes' section whose lead is broadcast text and
    whose only subsection is a spin-off doesn't count, so the season articles are used."""
    fake = season_fake()
    w = fake.data["wikipedia"]
    w["sections:91000"] = sections(MAIN, ["Episodes", "Cast"])
    lead = " ".join(["The series premiered on the network and aired weekly; ratings fell and the "
                     "showrunner was renewed for a second season by critics."] * 12)
    w["text:91000:1"] = {"parse": {"text": (
        '<div class="mw-parser-output"><div class="mw-heading mw-heading2"><h2>Episodes</h2>'
        f"</div><p>{lead}</p>"
        '<div class="mw-heading mw-heading3"><h3>Harbor Lights spin-off</h3></div>'
        f"<p>{words(200, 'spin')}</p></div>")}}
    rec = fetch(fake)
    assert rec["status"] == "ok" and rec["via"] == "season_articles"
    main = rec["main_article"]
    assert main["skip_reason"] == "too_short"
    assert "after non-plot parts were dropped" in main["skip_detail"]
    check = main["section_checks"][0]
    assert check["heading"] == "episodes" and check["accepted"] is False
    assert {d["heading"] for d in check["dropped"]} == {"episodes", "harbor lights spin-off"}
    assert "section_checks" not in rec  # moved under main_article
    for part in rec["sources"]:
        assert part["section_checks"][-1]["accepted"] is True
