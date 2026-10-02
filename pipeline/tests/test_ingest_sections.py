"""Non-plot parts of series plot sections (fetcher 1.4.0, DECISIONS 2026-10-02).

Fixtures in ``fixtures/ingest/sections`` are trimmed copies of real English Wikipedia section
HTML from the 2026-10-02 pilot ingestion (CC BY-SA; article, revision and license in
``sources.json``), so the rules are tested on the markup and prose QA found.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from laminary_pipeline.ingest.sections import (
    filter_section,
    is_plot_heading,
    nonplot_heading,
    nonplot_share,
    split_parts,
)
from laminary_pipeline.ingest.text import html_to_text, word_count

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ingest" / "sections"


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


def reasons(f: object) -> dict[str, str]:
    return {d["heading"]: d["reason"] for d in f.dropped}  # type: ignore[attr-defined]


def test_fixture_sources_are_attributed() -> None:
    meta = json.loads((FIXTURES / "sources.json").read_text(encoding="utf-8"))
    names = {p.stem for p in FIXTURES.glob("*.html")}
    assert names == set(meta)
    for entry in meta.values():
        assert entry["permalink"].startswith("https://en.wikipedia.org/w/index.php?title=")
        assert entry["license"].startswith("CC-BY-SA")


# --- real sections QA flagged -----------------------------------------------------------------


def test_seinfeld_series_overview_is_all_non_plot() -> None:
    f = filter_section(fixture("seinfeld_series_overview"), "series overview")
    assert not f.accepted and f.text == "" and f.words == 0
    r = reasons(f)
    assert "plotlines" not in r  # a plot heading now: content-checked and kept, but too small
    for heading in ("themes", "catchphrases", "consumer products", "music"):
        assert r[heading].startswith("heading names non-plot content")
    assert "mostly non-plot" in (f.reason or "")


def test_lost_keeps_seasons_and_drops_mythology_and_its_subsection() -> None:
    html = fixture("lost_series_overview")
    f = filter_section(html, "series overview")
    assert f.accepted
    r = reasons(f)
    assert set(r) == {"mythology and interpretations", "recurring elements"}
    assert r["recurring elements"] == "under dropped subsection 'mythology and interpretations'"
    assert f.text.startswith("Season 1 begins with the aftermath of a plane crash")
    dropped_words = sum(d["words"] for d in f.dropped)
    assert f.words + dropped_words == f.words_before == word_count(html_to_text(html))


def test_doctor_who_episode_list_lead_and_missing_episodes_are_dropped() -> None:
    f = filter_section(fixture("doctor_who_episodes"), "episodes")
    assert not f.accepted and f.words == 0
    r = reasons(f)
    assert r["episodes"].startswith("episode-list lead under 'episodes', content: ")
    assert r["missing episodes"] == "heading names non-plot content ('missing episodes')"


def test_lost_fan_theories_still_go_but_x_files_mythology_stays() -> None:
    """QA: 'mythology' is The X-Files' main storyline, so it is no longer denylisted; Lost's
    'Mythology and interpretations' still goes on 'interpretations'."""
    xf = filter_section(fixture("x_files_premise"), "premise")
    assert xf.accepted and xf.dropped == [] and xf.words == xf.words_before
    assert "mythology" in [p.heading for p in split_parts(fixture("x_files_premise"))]
    lost = reasons(filter_section(fixture("lost_series_overview"), "series overview"))
    assert lost["mythology and interpretations"] == (
        "heading names non-plot content ('interpretations')")


def test_star_trek_tng_production_seasons_fail_the_content_check() -> None:
    f = filter_section(fixture("star_trek_tng_seasons"), "seasons")
    assert not f.accepted
    r = reasons(f)
    assert r["legacy"].startswith("heading names non-plot content")
    assert r["season 2 (1988–1989)"].startswith("content: ")
    assert sum(1 for k in r if k.startswith("season ")) >= 5


def test_better_call_saul_keeps_every_season_and_drops_the_lead() -> None:
    """Each season part opens with its renewal/premiere paragraph: that paragraph goes, the
    season summary after it stays."""
    f = filter_section(fixture("better_call_saul_episodes"), "episodes")
    assert f.accepted
    assert f.dropped[0]["heading"] == "episodes"
    trimmed = [d for d in f.dropped if d.get("paragraph") == 1]
    assert [d["heading"] for d in trimmed] == [
        "season 1 (2015)", "season 2 (2016)", "season 3 (2017)", "season 4 (2018)",
        "season 5 (2020)", "season 6 (2022)"]
    for d in trimmed:
        assert d["reason"].startswith("first paragraph of a season part")
    assert "renewed" not in f.text and "premiered" not in f.text
    assert f.words + sum(d["words"] for d in f.dropped) == f.words_before
    assert f.words > 0.5 * f.words_before


def test_friends_plot_paragraph_with_cue_words_is_untouched() -> None:
    """Friends season 2's third paragraph has cue words (an interview, the show's writer) but
    is plot; only a season's first paragraph is ever cut, so nothing changes."""
    html = fixture("friends_episodes")
    f = filter_section(html, "episodes")
    assert f.accepted and f.dropped == [] and f.text == html_to_text(html)


def test_scrubs_overview_is_production_writing() -> None:
    f = filter_section(fixture("scrubs_overview"), "overview")
    assert not f.accepted and reasons(f)["overview"].startswith("content: 100%")


def test_sherlock_episodes_is_mostly_non_plot_so_rejected_whole() -> None:
    f = filter_section(fixture("sherlock_episodes"), "episodes")
    assert not f.accepted and f.text == ""
    r = reasons(f)
    assert r["christmas mini-episode (2013)"].startswith("not a plot heading")
    assert r["future"].startswith("heading names non-plot content")


# --- rules on synthetic markup ----------------------------------------------------------------


def section(top: str, parts: list[tuple[int, str, str]], lead: str = "") -> str:
    out = [f'<div class="mw-parser-output"><div class="mw-heading mw-heading2"><h2>{top}</h2>'
           "</div>"]
    if lead:
        out.append(f"<p>{lead}</p>")
    for level, heading, body in parts:
        out.append(f'<div class="mw-heading mw-heading{level}"><h{level} id="x">{heading}'
                   f"</h{level}></div><p>{body}</p>")
    out.append("</div>")
    return "".join(out)


STORY = ("Mara inherits her uncle's failing boatyard and must keep it afloat while a rival "
         "family schemes to buy the waterfront. ") * 6
PRODUCTION = ("The season premiered on the network in March and aired weekly; ratings rose, "
              "the showrunner said in an interview, and critics praised the writers. ") * 3


def test_nothing_dropped_gives_exactly_the_old_text() -> None:
    html = section("Plot", [(3, "Season 1", STORY), (3, "Season 2", STORY)], lead=STORY)
    f = filter_section(html, "plot")
    assert f.accepted and f.dropped == [] and f.text == html_to_text(html)


def test_split_parts_rejoins_to_the_original_html() -> None:
    html = section("Episodes", [(3, "Season 1", STORY), (4, "Part 1", STORY)], lead="Lead.")
    parts = split_parts(html)
    assert "".join(p.html for p in parts) == html
    assert [(p.level, p.heading, p.parent) for p in parts] == [
        (2, "episodes", None), (3, "season 1", 0), (4, "part 1", 1)]


def test_plot_heading_subsections_are_kept_without_content_check() -> None:
    """Under a plot heading, a 'Season N' subsection is story even if it mentions ratings."""
    html = section("Plot", [(3, "Season 1", PRODUCTION + STORY), (3, "Season 2", STORY)])
    f = filter_section(html, "plot")
    assert f.accepted and f.dropped == []


def test_neutral_subsection_under_plot_heading_gets_the_content_check() -> None:
    html = section("Premise", [(3, "Setting", STORY), (3, "Broadcast notes", STORY),
                               (3, "Reception notes", PRODUCTION), (3, "General", PRODUCTION)],
                   lead=STORY)
    r = reasons(filter_section(html, "premise"))
    assert r["broadcast notes"] == "heading names non-plot content ('broadcast')"
    assert r["general"].startswith("content: ")
    assert "setting" not in r


def test_broad_heading_keeps_only_plot_headings() -> None:
    html = section("Series overview", [(3, "Season 1", STORY), (3, "Christmas special", STORY),
                                       (3, "Season 2", PRODUCTION)], lead=STORY)
    f = filter_section(html, "series overview")
    r = reasons(f)
    assert "christmas special" not in r  # specials: content check, and this one is story
    assert r["season 2"].startswith("first paragraph of a season part")
    assert "series overview" not in r  # a story lead under an overview heading is kept
    assert f.accepted


def test_specials_and_neutral_headings_under_a_broad_section() -> None:
    html = section("Episodes", [(3, "Season 1", STORY), (3, "Specials", ""),
                                (4, "Christmas Carol", STORY), (3, "Specials (2020)", PRODUCTION),
                                (3, "Missing episodes", STORY)], lead=STORY)
    f = filter_section(html, "episodes")
    r = reasons(f)
    assert "specials" not in r and "christmas carol" not in r
    assert r["specials (2020)"].startswith("content: ")
    assert r["missing episodes"].startswith("heading names non-plot content")
    assert "episodes" not in r  # a story lead passes the stricter episode-lead check


def test_dropped_empty_parent_heading_is_logged() -> None:
    html = section("Premise", [(3, "Production", ""), (4, "Filming", STORY)], lead=STORY)
    f = filter_section(html, "premise")
    assert [(d["heading"], d["words"]) for d in f.dropped] == [("production", 0),
                                                              ("filming", f.dropped[1]["words"])]
    assert f.dropped[1]["reason"] == "under dropped subsection 'production'"


def test_plot_heading_wins_over_the_word_denylist() -> None:
    assert nonplot_heading("season 5: revival (2016)") is None
    assert nonplot_heading("series 2: the reboot") is None
    assert nonplot_heading("revival") == "revival"
    html = section("Episodes", [(3, "Season 5: Revival (2016)", STORY)], lead=STORY)
    assert filter_section(html, "episodes").dropped == []


def test_unrecognised_heading_markup_fails_closed() -> None:
    html = ('<div class="mw-parser-output"><h2>Episodes</h2><p>' + STORY + "</p>"
            "<h3>Season 1</h3><p>" + STORY + "</p></div>")
    f = filter_section(html, "episodes")
    assert not f.accepted and f.markup_error and f.text == "" and f.words == 0
    assert f.record("3")["markup_error"] is True
    assert "mw-heading" in (f.reason or "")
    mixed = section("Plot", [(3, "Season 1", STORY)]) + "<h3>Season 2</h3><p>x</p>"
    assert filter_section(mixed, "plot").markup_error
    film = filter_section(html, "plot", "movie")  # films are used whole, as before
    assert film.accepted and not film.markup_error


def test_broad_section_mostly_dropped_is_rejected() -> None:
    html = section("Seasons", [(3, "Season 1", STORY), (3, "Production", STORY * 3)])
    f = filter_section(html, "seasons")
    assert not f.accepted and f.words == 0 and "mostly non-plot" in (f.reason or "")


def test_deeper_neutral_heading_under_a_kept_season_is_content_checked_not_dropped() -> None:
    html = section("Episodes", [(3, "Season 2", ""), (4, "The long night", STORY)])
    f = filter_section(html, "episodes")
    assert f.accepted and f.dropped == []


def test_films_are_used_whole() -> None:
    """Sholay's plot has 'Theatrical release (1975)' and "Director's cut" subsections: story."""
    html = section("Plot", [(3, "Theatrical release (1975)", STORY), (3, "Director's cut",
                                                                       STORY)])
    f = filter_section(html, "plot", "movie")
    assert f.kind == "film" and f.dropped == [] and f.text == html_to_text(html)


@pytest.mark.parametrize(
    ("heading", "denied"),
    [("production", "production"), ("cast and characters", "cast"), ("themes", "themes"),
     ("mythology and interpretations", "interpretations"), ("mythology", None),
     ("home media", "home media"),
     ("u.s. television ratings", "ratings"), ("cid special bureau", None),
     ("future trunks saga", None), ("future", "future"), ("story and characters", None),
     ("characters", "characters"), ("episodes 1–9, completing the laura palmer arc", None),
     ("25th anniversary documentary", "anniversary"), ("crossover with third watch",
                                                       "crossover")],
)
def test_heading_denylist(heading: str, denied: str | None) -> None:
    assert nonplot_heading(heading) == denied


@pytest.mark.parametrize(
    ("heading", "plot_like"),
    [("season 1 (2008)", True), ("seasons 1–3", True), ("series 2: blackadder ii", True),
     (" part i", True), ("saiyan saga", True), ("apophis arc", True), ("plot summary", True),
     ("plotlines", True), ("missing episodes", False), ("christmas mini-episode (2013)",
                                                         False)],
)
def test_plot_headings(heading: str, plot_like: bool) -> None:
    assert is_plot_heading(heading.strip()) is plot_like


def test_content_check_needs_two_cues_per_paragraph() -> None:
    assert nonplot_share("The episode aired on NBC.") == 0.0  # one cue only
    assert nonplot_share(PRODUCTION) == 1.0
    assert nonplot_share(STORY) == 0.0
    mixed = STORY + "\n\n" + PRODUCTION
    assert 0 < nonplot_share(mixed) < 0.5
