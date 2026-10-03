"""Fetcher 1.5.8 (DECISIONS 2026-10-02): trivia stored apart from the plot, a single pilot as
episode 0 of season 1, and the Seinfeld episode-text override.

Real, trimmed fixtures (``fixtures/ingest/episodes``, licence and permalink in
``sources.json``): "List of Midsomer Murders episodes" (Pilot (1997), Series 1) and "List of
Baywatch episodes" (Pilot TV movie (1989), Season 1). The trivia sentences in the rule tests are
quoted from the 2026-10-02 plot files (M*A*S*H, Baywatch, Midsomer Murders, NCIS, Xena, Only
Murders in the Building, The Good Place, Doctor Who, CID).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_ingest_episodes import (
    SERIES,
    TWO_SEASONS,
    _request,
    episode_fake,
    fetch,
    fixture,
    heading,
    season_page_html,
    table,
)
from test_ingest_seasons import words

from laminary_pipeline.annotate.prompt import labeler_text
from laminary_pipeline.gold.template import labeler_text_for
from laminary_pipeline.ingest import priority
from laminary_pipeline.ingest.episodes import (
    list_page_seasons,
    parse_tables,
    season_episodes,
    split_sentences,
    split_trivia,
    trivia_rule,
)
from laminary_pipeline.ingest.paths import DataPaths, write_json_atomic
from laminary_pipeline.ingest.plots import needs_fetch
from laminary_pipeline.ingest.priority import PREFER_EPISODE_TEXT
from laminary_pipeline.ingest.text import sha256_text, word_count
from laminary_pipeline.ingest.wikipedia import FETCHER_VERSION

# --- trivia rules ---------------------------------------------------------------------------


@pytest.mark.parametrize(("sentence", "rule"), [
    ("Note: This is the first episode to feature Sean Murray as Timothy McGee.", "note"),
    ("Notes: First appearance of Garner Ellerbee (Gregory Alan Williams).", "note"),
    ("Note – the episode was filmed on location.", "note"),
    ('Montage music: "Life is a Carnival" - The Band', "montage_music"),
    ("Carl Kleinschmitt received a Writers Guild Award nomination for this episode.", "award"),
    ("Hy Averback received Primetime Emmy and Directors Guild Award nominations for this "
     "episode.", "award"),
    ("TV Guide ranked this episode number 12 on its list of the greatest episodes.", "award"),
    ("This episode marks the first appearance of William Christopher as Father Francis "
     "Mulcahy.", "first_appearance"),
    ("Allan Arbus makes his first appearance as Dr. Sidney Freedman.", "first_appearance"),
    ("Anna Massey, Joanna David and Una Stubbs also appear.", "also_appears"),
    ("Toby Jones also appears.", "also_appears"),
])
def test_trivia_sentences_are_recognised(sentence: str, rule: str) -> None:
    assert trivia_rule(sentence) == rule


@pytest.mark.parametrize("sentence", [
    'Mabel comes across a note from Tim detailing a meeting with somebody known as "G.M.".',
    "Rose notices the chips have an adverse effect, while the Doctor notes the chips seem to "
    "make the students more intelligent.",
    "Later on the set, Riya gets a threatening note.",
    "The ghost of his brother also appears to Sam at the funeral.",
    "Hawkeye won the poker game and the award for the best still.",
    "Gerald Hadleigh (Robert Swann), chairman of the writers circle, is deeply troubled.",
    "She makes her first move on the case.",
])
def test_plot_sentences_stay(sentence: str) -> None:
    assert trivia_rule(sentence) is None


def test_sentences_do_not_split_after_titles() -> None:
    assert split_sentences("Allan Arbus makes his first appearance as Dr. Sidney Freedman. "
                           "Hawkeye is drafted.") == [
        "Allan Arbus makes his first appearance as Dr. Sidney Freedman.", "Hawkeye is drafted."]


def test_split_trivia_keeps_plot_and_moves_notes() -> None:
    text = ("Hobie skips summer school (Brandon Call). He goes power skiing.\n\n"
            "Notes: First appearance of Garner Ellerbee. He returns later.\n\n"
            'Montage music: "Life is a Carnival" - The Band')
    plot, trivia = split_trivia(text)
    assert plot == "Hobie skips summer school (Brandon Call). He goes power skiing."
    assert [r for r, _ in trivia] == ["note", "montage_music"]
    assert trivia[0][1] == "Notes: First appearance of Garner Ellerbee. He returns later."


# --- real markup: pilots and trivia ---------------------------------------------------------


def test_midsomer_pilot_is_episode_0_of_series_1_and_cast_lists_are_trivia() -> None:
    seasons, skipped = list_page_seasons(parse_tables(fixture("midsomer_pilot_episodes")))
    assert skipped == [] and [(n, len(ts)) for n, ts in seasons] == [(1, 2)]
    se = season_episodes(1, seasons[0][1])
    assert [e.marker for e in se.episodes] == ["S1E0", "S1E1", "S1E2"]
    assert se.episodes[0].paragraph.startswith(
        'S1E0 "The Killings at Badger\'s Drift": The death of elderly Miss Emily Simpson '
        "(Renée Asherson)")  # actor names in parentheses stay in the plot
    assert all("also appear" not in e.summary for e in se.episodes)
    assert [(t.marker, t.rule) for t in se.trivia] == [
        ("S1E0", "also_appears"), ("S1E1", "also_appears"), ("S1E2", "also_appears")]
    assert se.rows == 3 and se.without_summary == 0


def test_baywatch_pilot_movie_is_episode_0_and_notes_and_music_are_trivia() -> None:
    seasons, _ = list_page_seasons(parse_tables(fixture("baywatch_pilot_episodes")))
    se = season_episodes(1, seasons[0][1])
    assert [e.marker for e in se.episodes] == ["S1E0", "S1E1", "S1E2"]
    assert se.episodes[0].paragraph.startswith("S1E0 Panic at Malibu Pier: Mitch Buchannon")
    assert {t.rule for t in se.trivia} == {"note", "montage_music"}
    for e in se.episodes:
        assert "Montage" not in e.summary and not e.summary.startswith("Notes")


def test_several_pilot_entries_or_a_late_pilot_are_not_numbered() -> None:
    from test_ingest_episodes import ep_rows

    two = (heading(3, "Pilots") + table(ep_rows(1, 2, 20)) + heading(3, "Season 1")
           + table(ep_rows(1, 2, 20)))
    seasons, skipped = list_page_seasons(parse_tables(two))
    assert [(n, len(ts)) for n, ts in seasons] == [(1, 1)]
    assert skipped == [{"heading": "pilots", "reason": "2 pilot entries: only a single pilot "
                        "is numbered as episode 0"}]
    late = (heading(3, "Season 1") + table(ep_rows(1, 2, 20)) + heading(3, "Pilot")
            + table(ep_rows(1, 1, 20)))
    seasons, skipped = list_page_seasons(parse_tables(late))
    assert [(n, len(ts)) for n, ts in seasons] == [(1, 1)]
    assert "after the seasons began" in skipped[0]["reason"]


# --- the plot file: trivia stored apart, never in the model or gold input -------------------

TRIVIA = " Note: This is the first episode to feature the lighthouse keeper."


def trivia_fake() -> Any:
    pages = dict(TWO_SEASONS)
    pages[1] = season_page_html(1, 3, 40).replace(words(40, "s1e2w"),
                                                  words(40, "s1e2w") + TRIVIA, 1)
    return episode_fake(season_pages=pages)


def test_trivia_is_stored_with_attribution_and_kept_out_of_the_text() -> None:
    rec = fetch(trivia_fake())
    assert rec["status"] == "ok" and rec["fetcher_version"] == FETCHER_VERSION
    assert rec["trivia"] == [{
        "episode": "S1E2", "rule": "note", "text": TRIVIA.strip(), "words": 11,
        "ref": "https://en.wikipedia.org/wiki/Tidewater_season_1", "revision": "92010",
        "license": "CC-BY-SA-4.0"}]
    assert rec["episode_tables"]["trivia"] == {"items": 1, "words": 11, "by_rule": {"note": 1}}
    for part in rec["sources"]:
        assert "lighthouse keeper" not in part["text"]
        assert part["source"]["content_sha256"] == sha256_text(part["text"])
        assert part["source"]["word_count"] == word_count(part["text"])
    # the same series without the trivia sentence has exactly the same text
    plain = fetch(episode_fake(season_pages=TWO_SEASONS))
    assert [p["text"] for p in plain["sources"]] == [p["text"] for p in rec["sources"]]
    assert plain["trivia"] == []


def test_trivia_never_reaches_the_model_or_the_gold_text() -> None:
    rec = fetch(trivia_fake())
    gated, params = _request(rec)
    request_text = "".join(b["text"] for b in params["messages"][0]["content"])
    assert "lighthouse keeper" not in request_text and "Note:" not in request_text
    assert "lighthouse keeper" not in labeler_text(gated)
    assert "lighthouse keeper" not in labeler_text_for(rec)


# --- Seinfeld: episode text by a per-title override ------------------------------------------


def test_seinfeld_is_in_the_override() -> None:
    assert "Q23733" in PREFER_EPISODE_TEXT and priority.PRIORITY_SERIES["Q23733"] == "Seinfeld"


def _override_fake(monkeypatch, *, seasons: bool = True) -> Any:
    """Tidewater as a priority series in the override, with an 800-word main article (passes,
    over the 500-word richer-text threshold) and episode tables (shorter) on its season pages."""
    monkeypatch.setitem(priority.PRIORITY_SERIES, SERIES, "Tidewater")
    monkeypatch.setattr("laminary_pipeline.ingest.wikipedia.PREFER_EPISODE_TEXT",
                        frozenset({SERIES}))
    fake = episode_fake(season_pages=TWO_SEASONS if seasons else {})
    fake.data["wikipedia"]["text:92000:1"] = {"parse": {"text": (
        f"<div><p>{words(800, 'premise')}</p></div>")}}
    return fake


def test_override_uses_the_episode_text_even_when_shorter(monkeypatch) -> None:
    rec = fetch(_override_fake(monkeypatch))
    assert rec["status"] == "ok" and rec["via_detail"] == "episode_table"
    report = rec["richer_text"]
    assert report["reason"] == "per-title override" and report["chosen"] == "episode_table"
    assert report["main_article_words"] == 800
    assert 0 < report["alternative_words"] < 800 == report["main_article_words"]


def test_override_falls_back_to_the_main_article(monkeypatch) -> None:
    rec = fetch(_override_fake(monkeypatch, seasons=False))
    assert rec["status"] == "ok" and rec["word_count"] == 800 and "via" not in rec
    assert rec["richer_text"]["reason"] == "per-title override"
    assert rec["richer_text"]["chosen"] == "main_article"
    assert rec["richer_text"]["alternative_status"] == "skipped"


def test_without_the_override_a_long_main_article_is_kept(monkeypatch) -> None:
    fake = _override_fake(monkeypatch)
    monkeypatch.setattr("laminary_pipeline.ingest.wikipedia.PREFER_EPISODE_TEXT", frozenset())
    rec = fetch(fake)
    assert rec["word_count"] == 800 and "richer_text" not in rec


# --- re-fetch ---------------------------------------------------------------------------------


def test_episode_table_files_and_seinfeld_from_before_1_5_8_are_fetched_again(
        tmp_path: Path) -> None:
    paths = DataPaths.resolve(str(tmp_path))
    for rec in ({"qid": "Q751917", "status": "ok", "via_detail": "episode_table",
                 "episode_tables": {}},
                {"qid": "Q9", "status": "skipped", "skip_reason": "too_short",
                 "episode_tables": {}, "season_articles": {"used": []}},
                {"qid": "Q23733", "status": "ok", "word_count": 1214}):
        old = {**rec, "fetcher_version": "1.5.7", "candidate": {"media_type": "tv_series"}}
        write_json_atomic(paths.plot_file(rec["qid"]), old)
        assert needs_fetch(paths, rec["qid"], False), rec["qid"]
        write_json_atomic(paths.plot_file(rec["qid"]), {**old, "fetcher_version": "1.5.8"})
        assert not needs_fetch(paths, rec["qid"], False), rec["qid"]
