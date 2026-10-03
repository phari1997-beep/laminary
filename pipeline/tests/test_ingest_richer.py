"""Richer text for the ten priority series (fetcher 1.5.2, DECISIONS 2026-10-02).

A priority series whose main article passes with under 500 words also tries the season-article
/ episode-table path, and the longer text is used. Synthetic fixtures: Doctor Who's shape from
``test_ingest_runs`` (so the revival run rule is exercised on this path) and the non-priority
"Tidewater" series.
"""

from __future__ import annotations

from pathlib import Path

from test_ingest_episodes import episode_fake
from test_ingest_episodes import fetch as fetch_tidewater
from test_ingest_runs import CLASSIC_REVS, DW, dw_fake, fetch, fetched_revs
from test_ingest_seasons import section_html, words

from laminary_pipeline.annotate.inputs import parse_plot
from laminary_pipeline.ingest.paths import DataPaths, write_json_atomic
from laminary_pipeline.ingest.plots import needs_fetch
from laminary_pipeline.ingest.priority import PRIORITY_SERIES
from laminary_pipeline.ingest.wikipedia import RICHER_TEXT_WORDS, STUB_SEASON_WORDS


def _main_words(fake, n: int) -> None:
    fake.data["wikipedia"]["text:340000:1"] = section_html("Premise", words(n, "premise"))


def test_threshold_is_the_stub_season_size() -> None:
    assert RICHER_TEXT_WORDS == STUB_SEASON_WORDS == 500


def test_longer_season_text_is_chosen_for_a_thin_priority_series() -> None:
    fake = dw_fake()  # revival series 1 and 2: 600 words each
    _main_words(fake, 249)
    rec = fetch(fake)
    assert rec["status"] == "ok" and rec["via"] == "season_articles"
    assert [p["page_title"] for p in rec["sources"]] == ["Doctor Who series 1",
                                                         "Doctor Who series 2"]
    assert rec["richer_text"] == {
        "rule": "priority_series_richer_text", "threshold_words": 500,
        "chosen": "season_articles", "main_article_words": 249,
        "alternative_words": rec["word_count"], "alternative_status": "ok",
        "alternative_detail": None}
    assert rec["word_count"] > 249
    assert rec["main_article"]["skip_reason"] is None
    assert rec["main_article"]["word_count"] == 249
    # the Doctor Who revival rule applies on this path too
    assert rec["series_run_rule"] == "doctor_who_revival"
    assert not (fetched_revs(fake) & CLASSIC_REVS)
    assert rec["coverage"]["total_seasons"] == 3
    parse_plot(rec, "t")  # valid annotation input


def test_main_article_is_kept_when_it_is_longer() -> None:
    fake = dw_fake(prose=False)  # episode tables only: 2 revival seasons of short summaries
    _main_words(fake, 450)
    rec = fetch(fake)
    assert rec["status"] == "ok" and "via" not in rec
    assert rec["text"].startswith("premise0 ") and rec["word_count"] == 450
    report = rec["richer_text"]
    assert report["chosen"] == "main_article" and report["main_article_words"] == 450
    assert 0 < report["alternative_words"] < 450 and report["alternative_status"] == "ok"
    assert rec["series_run_rule"] == "doctor_who_revival"
    parse_plot(rec, "t")


def test_main_article_is_kept_when_the_other_path_fails() -> None:
    fake = dw_fake(prose=False, verified=())  # nothing verifies
    fake.data["sparql"]["list_statements"] = {"results": {"bindings": [
        {"item": {"value": "http://www.wikidata.org/entity/Q9360005"},
         "prop": {"value": "P361"}, "value": {"value": "http://www.wikidata.org/entity/Q1"}}]}}
    _main_words(fake, 249)
    rec = fetch(fake)
    assert rec["status"] == "ok" and rec["word_count"] == 249
    assert rec["richer_text"]["chosen"] == "main_article"
    assert rec["richer_text"]["alternative_words"] == 0
    assert rec["richer_text"]["alternative_status"] == "skipped"
    assert rec["richer_text"]["alternative_detail"]


def test_a_priority_series_over_the_threshold_is_not_retried() -> None:
    fake = dw_fake()
    _main_words(fake, 500)
    rec = fetch(fake)
    assert "richer_text" not in rec and rec["word_count"] == 500
    assert not any(r.host == "query.wikidata.org" for r in fake.requests)


def test_non_priority_series_are_untouched() -> None:
    fake = episode_fake()
    fake.data["wikipedia"]["text:92000:1"] = {"parse": {"text": f"<div><p>{words(249, 'p')}"
                                                                "</p></div>"}}
    rec = fetch_tidewater(fake)
    assert "Q9200000" not in PRIORITY_SERIES
    assert rec["status"] == "ok" and rec["word_count"] == 249 and "richer_text" not in rec
    assert not any(r.host == "query.wikidata.org" for r in fake.requests)


def test_thin_priority_series_from_older_fetchers_are_fetched_again(tmp_path: Path) -> None:
    paths = DataPaths.resolve(str(tmp_path))
    for qid, n in (("Q192837", 180), (DW, 249), ("Q16290", 278), ("Q494244", 345)):
        old = {"qid": qid, "status": "ok", "word_count": n, "fetcher_version": "1.5.0",
               "candidate": {"media_type": "tv_series"}}
        write_json_atomic(paths.plot_file(qid), old)
        assert needs_fetch(paths, qid, False), qid
        write_json_atomic(paths.plot_file(qid), {**old, "fetcher_version": "1.5.2"})
        assert not needs_fetch(paths, qid, False), qid
    # 500+ words, a season-article file, or a non-priority series: not fetched again
    for rec in ({"qid": "Q23733", "word_count": 800},
                {"qid": "Q23733", "word_count": 300, "via": "season_articles"},
                {"qid": "Q1", "word_count": 300}):
        full = {"status": "ok", "fetcher_version": "1.5.0",
                "candidate": {"media_type": "tv_series"}, **rec}
        write_json_atomic(paths.plot_file(rec["qid"]), full)
        assert not needs_fetch(paths, rec["qid"], False), rec
