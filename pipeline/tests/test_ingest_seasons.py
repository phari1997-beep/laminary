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


SEASON_WORDS = {1: 120, 2: 130, 4: 2900}


def season_fake(
    *, ordinals: dict[str, int] | None = None, season_words: dict[int, int] | None = None
) -> FakeWikimedia:
    """A thin main article and its seasons:

    - season 1 ("Tidewater season 1", also reached via the redirect "Tidewater (season 1)"):
      Plot, 120 words.
    - season 2 ("Tidewater (season 2)", found only through a link in the main article):
      an Episodes table, a table-only Season overview, then Synopsis with 130 words.
    - season 3: Wikidata doesn't place it in the series, so it is unverifiable.
    - season 4: verified, 2,900 words: crosses the 3,000-word cap.
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
    assert rec["word_count"] == 250 == sum(p["source"]["word_count"] for p in parts)
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
    """With a 200-word cap, season 2 (130 words) would cross it after season 1 (120): it is
    left out whole, and the 120 words left fail the 150-word rule on the joined text."""
    rec = fetch(season_fake(), season_word_cap=200)
    assert rec["status"] == "skipped" and rec["skip_reason"] == "too_short"
    assert rec["season_articles"]["used"] == ["Tidewater season 1"]
    assert rec["season_articles"]["left_out_over_cap"] == [
        "Tidewater (season 2)", "Tidewater season 4"
    ]
    assert "1 usable with 120 words" in rec["skip_detail"]


def test_ordinal_that_disagrees_with_the_title_skips_the_page() -> None:
    rec = fetch(season_fake(ordinals={"Q9100002": 5}))
    reasons = {s["title"]: s["reason"] for s in rec["season_articles"]["skipped"]}
    assert "disagrees" in reasons["Tidewater (season 2)"]
    assert rec["status"] == "skipped"  # season 1 alone is 120 words


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


def test_season_lookup_can_be_turned_off() -> None:
    rec = fetch(season_fake(), season_articles=False)
    assert rec["status"] == "skipped" and "season_articles" not in rec


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
    """Cost re-check: the bound covers all sources and markers, up to the 3,000-word cap."""
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
    assert gold["provenance"]["input_word_count"] == 250


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
    rec["provenance"]["input_word_count"] = 250
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


# --- first-season exception (DECISIONS 2026-10-01) -----------------------------------------


def test_season_one_over_the_cap_is_used_alone_and_whole() -> None:
    rec = fetch(season_fake(season_words={1: 3500}))
    assert rec["status"] == "ok"
    assert [p["page_title"] for p in rec["sources"]] == ["Tidewater season 1"]
    assert rec["word_count"] == 3500 == word_count(rec["sources"][0]["text"])  # not cut
    assert rec["season_articles"]["left_out_over_cap"] == [
        "Tidewater (season 2)", "Tidewater season 4"
    ]


def test_a_later_season_over_the_cap_is_excluded() -> None:
    rec = fetch(season_fake(season_words={1: 200, 2: 3200}))
    assert rec["status"] == "ok"
    assert [p["page_title"] for p in rec["sources"]] == ["Tidewater season 1"]
    assert rec["word_count"] == 200
    assert rec["season_articles"]["left_out_over_cap"][0] == "Tidewater (season 2)"


def test_season_one_over_the_ceiling_skips_the_title() -> None:
    from laminary_pipeline.ingest.seasons import SEASON_ONE_CEILING

    assert SEASON_ONE_CEILING == 6000
    rec = fetch(season_fake(season_words={1: 6001}))
    assert rec["status"] == "skipped" and rec["skip_reason"] == "season_too_long"
    assert "Tidewater season 1 (6001 words)" in rec["skip_detail"]
    assert not rec["season_articles"]["used_list_page"]  # no fallback to the list page
    ok = fetch(season_fake(season_words={1: 6000}))
    assert ok["status"] == "ok" and ok["word_count"] == 6000


# --- gate limits on multi-source input (QA should-fix 1, probe7) ---------------------------


def _plot_with(texts: list[str], *, seasons: bool = True):
    from annotate_support import source_meta

    from laminary_pipeline.annotate.inputs import PlotInput, PlotSource

    srcs = []
    for i, text in enumerate(texts, 1):
        meta = source_meta(text, ref=f"https://en.wikipedia.org/wiki/Tidewater_season_{i}")
        if seasons:
            meta["season"] = i
        srcs.append(PlotSource(meta, text))
    return PlotInput({"media_type": "tv_series", "tmdb_id": 91000}, tuple(srcs), "t")


def test_gate_refuses_more_sources_than_a_record_holds() -> None:
    from laminary_pipeline.annotate.inputs import max_sources

    assert max_sources() == 20
    with pytest.raises(GateError, match="21 sources, a record holds at most 20"):
        gate(_plot_with([words(10, f"p{i}w") for i in range(21)]))
    assert len(gate(_plot_with([words(10, f"p{i}w") for i in range(20)])).plot.sources) == 20


def test_gate_refuses_multi_source_input_over_the_cap() -> None:
    with pytest.raises(GateError, match="over the 3000-word season-article cap"):
        gate(_plot_with([words(1600, "a"), words(1600, "b")]))
    gate(_plot_with([words(1500, "a"), words(1500, "b")]))  # exactly at the cap


def test_gate_bounds_a_lone_season_by_the_ceiling() -> None:
    gate(_plot_with([words(6000, "a")]))  # season-1 exception
    with pytest.raises(GateError, match="over the 6000-word ceiling"):
        gate(_plot_with([words(6001, "a")]))
    gate(_plot_with([words(6001, "a")], seasons=False))  # a main article has no upper limit
