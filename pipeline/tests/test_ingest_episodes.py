"""Episode-table fallback for big series (fetcher 1.5.0, DECISIONS 2026-10-02).

Two kinds of fixture:

- Real, trimmed English Wikipedia HTML from the local HTTP cache (``fixtures/ingest/episodes``,
  licence and permalink in its ``sources.json``): a season page's episode table (Twin Peaks
  season 3) and episode tables under season headings with a rowspan continuation row and
  episodes without a summary (Shrinking). None of the ten priority shows' season or episode-list
  pages were in the cache, so they are not among the fixtures.
- Hand-built ``wikiepisodetable`` HTML for the fictional series "Tidewater", modelled on the real
  markup (number cells, ``td.summary`` title, director, writer, air date, viewers and production
  code cells, ``tr.expand-child`` / ``td.description`` summary rows), with synthetic words as
  summaries. Used to drive the whole fetcher, the gate, gold and the reports.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import urllib.parse
from pathlib import Path
from typing import Any

import pytest
from test_ingest_seasons import NOW, page, sections, words
from wikimedia_fake import FakeWikimedia

from laminary_pipeline.annotate.config import DEFAULT_PROMPT_VERSION
from laminary_pipeline.annotate.inputs import (
    GateError,
    InputFormatError,
    gate,
    parse_plot,
)
from laminary_pipeline.annotate.prompt import (
    build_request,
    labeler_coverage,
    labeler_text,
    load_prompt,
    prompt_coverage,
    source_block_indices,
    verify_request,
)
from laminary_pipeline.annotation import validate_record
from laminary_pipeline.gold.template import manifest_rows, template_csv, template_rows
from laminary_pipeline.ingest.episodes import (
    SeasonEpisodes,
    join_episodes,
    list_page_seasons,
    parse_tables,
    season_episodes,
    season_page_tables,
)
from laminary_pipeline.ingest.http import HttpClient
from laminary_pipeline.ingest.paths import DataPaths, write_json_atomic
from laminary_pipeline.ingest.plots import (
    PRIORITY_SERIES,
    effective_pilot,
    format_report,
    plots_report,
    run_plots,
)
from laminary_pipeline.ingest.text import sha256_text, word_count
from laminary_pipeline.ingest.wikipedia import FETCHER_VERSION, PlotFetcher

TAIL = "; the summary stops before that season ends)."  # the partial-season line's ending
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ingest" / "episodes"
SERIES = "Q9200000"
MAIN = "Tidewater (TV series)"
# cells that must never reach the text: director, writer, air date, viewers, production code
NON_SUMMARY = ("Ann Director", "Bo Writer", "March 3, 1991", "12.34", "4X5501")


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


# --- hand-built wikiepisodetable HTML (synthetic summaries) ---------------------------------


def ep_rows(season: int, n: int, size: int, *, first_overall: int = 1, titles: bool = True,
            summaries: bool = True, cite_error: bool = False) -> str:
    out = []
    for i in range(1, n + 1):
        title = (f'<td class="summary" rowspan="1" style="text-align:left">"<a href="/wiki/E{i}">'
                 f'Episode {season}-{i}</a>"</td>' if titles
                 else '<td class="summary" rowspan="1" style="text-align:left"></td>')
        out.append(
            '<tr class="vevent module-episode-list-row" style="text-align:center">'
            f'<th scope="row" rowspan="1" id="ep{first_overall + i - 1}">{first_overall + i - 1}'
            f'</th><td style="text-align:center">{i}</td>{title}'
            '<td style="text-align:center"><a href="/wiki/Ann_Director">Ann Director</a></td>'
            '<td style="text-align:center">Bo Writer</td>'
            '<td style="text-align:center">March&#160;3,&#160;1991<span style="display: none;">'
            '&#160;(<span class="bday dtstart published updated itvstart">1991-03-03</span>)'
            '</span></td><td style="text-align:center">12.34<sup class="reference">'
            '<a href="#cite_note-1">[1]</a></sup></td>'
            '<td style="text-align:center">4X5501</td></tr>'
        )
        if summaries:
            err = ('<span class="error mw-ext-cite-error" lang="en">Cite error: There are '
                   '&lt;ref&gt; tags on this page without content in them.</span>'
                   if cite_error and i == 1 else "")
            out.append(
                '<tr class="expand-child"><td class="description" colspan="8">'
                '<div class="shortSummaryText" style="max-width:90vw">'
                f'{words(size, f"s{season}e{i}w")}<sup class="reference"><a>[2]</a></sup>{err}'
                '</div></td></tr>'
            )
    return "\n".join(out)


HEADER_ROW = (
    '<tr style="color:white;text-align:center"><th scope="col">No.<br />overall</th>'
    '<th scope="col">No. in<br />season</th><th scope="col">Title</th>'
    '<th scope="col">Directed by</th><th scope="col">Written by</th>'
    '<th scope="col">Original release date</th><th scope="col">U.S. viewers<br />(millions)</th>'
    '<th scope="col">Prod.<br />code</th></tr>'
)


def table(rows: str) -> str:
    return (f'<table class="wikitable plainrowheaders wikiepisodetable" style="width:100%">'
            f'<tbody>{HEADER_ROW}{rows}</tbody></table>')


def heading(level: int, text: str) -> str:
    return f'<div class="mw-heading mw-heading{level}"><h{level} id="x">{text}</h{level}></div>'


def season_page_html(season: int, n: int, size: int, **kw: Any) -> str:
    """A season page: lead prose, an Episodes table, a Specials table (skipped), cast prose,
    references."""
    return (
        '<div class="mw-content-ltr mw-parser-output">'
        f'<p>The {season} season of Tidewater premiered in 1991 to strong ratings.</p>'
        f'{heading(2, "Episodes")}'
        '<div role="note" class="hatnote">See also: List of Tidewater episodes</div>'
        f'{table(ep_rows(season, n, size, **kw))}'
        f'{heading(3, "Specials")}{table(ep_rows(season, 1, 30, first_overall=99))}'
        f'{heading(2, "Cast")}<p>Ann Actor as the keeper.</p>'
        '<div class="mw-references-wrap"><ol class="references"><li>Ratings source.</li></ol>'
        '</div></div>'
    )


def list_page_html(seasons: dict[int, tuple[int, int]], *, restart: bool = False) -> str:
    """An episode-list page: a series overview table (not an episode table), then one table per
    season under "Season N (year)" headings, then specials; ``restart`` adds a revival that
    starts again at "Series 1"."""
    body = [
        '<div class="mw-content-ltr mw-parser-output">',
        heading(2, "Series overview"),
        '<table class="wikitable plainrowheaders"><tr><th>Season</th><th>Episodes</th></tr>'
        '<tr><td>1</td><td>3</td></tr></table>',
        heading(2, "Episodes"),
    ]
    overall = 1
    for n, (count, size) in seasons.items():
        body += [heading(3, f"Season {n} ({1990 + n})"),
                 table(ep_rows(n, count, size, first_overall=overall))]
        overall += count
    body += [heading(2, "Specials"), table(ep_rows(9, 1, 40, first_overall=900))]
    if restart:
        body += [heading(2, "Revival"), heading(3, "Series 1 (2005)"),
                 table(ep_rows(1, 2, 40, first_overall=1000))]
    body.append("</div>")
    return "".join(body)


def episode_fake(
    *,
    season_pages: dict[int, str] | None = None,
    list_html: str | None = None,
    prose_words: int = 0,
) -> FakeWikimedia:
    """A series whose main article is too thin; its season pages 1, 2 and 4 are verified
    (P179 with ordinals), season 3 is not; "List of Tidewater episodes" is verified (P361).
    Season pages have no plot prose (or ``prose_words`` of it), so the prose join fails and the
    episode tables are tried. ``season_pages`` maps a season to its full-page HTML; a season
    left out has no season page at all."""
    fake = FakeWikimedia()
    w = fake.data["wikipedia"]
    w[f"query:{MAIN}"] = page(MAIN, 9200, 92000, SERIES)
    w["sections:92000"] = sections(MAIN, ["Premise", "Cast"])
    w["text:92000:1"] = {"parse": {"text": f"<div><p>{words(40, 'premise')}</p></div>"}}
    revs = {1: 92010, 2: 92020, 3: 92030, 4: 92040}
    for n, html in (season_pages or {}).items():
        title, rev = f"Tidewater season {n}", revs[n]
        w[f"query:{title}"] = page(title, 9200 + n, rev, f"Q92000{n:02d}")
        heads = ["Plot", "Episodes"] if prose_words else ["Episodes", "Cast"]
        w[f"sections:{rev}"] = sections(title, heads)
        if prose_words:
            w[f"text:{rev}:1"] = {"parse": {"text": f"<div><p>{words(prose_words, f'p{n}w')}"
                                                    "</p></div>"}}
        w[f"page:{rev}"] = {"parse": {"text": html}}
    if list_html is not None:
        w["query:List of Tidewater episodes"] = page("List of Tidewater episodes", 9210, 92100,
                                                     "Q9200010")
        w["sections:92100"] = sections("List of Tidewater episodes", ["Series overview"])
        w["text:92100:1"] = {"parse": {"text": "<div><table><tr><td>1</td></tr></table></div>"}}
        w["page:92100"] = {"parse": {"text": list_html}}
    bindings = [
        {"item": {"value": f"http://www.wikidata.org/entity/Q92000{n:02d}"},
         "ordinal": {"value": str(n)}}
        for n in (1, 2, 4)
    ] + [{"item": {"value": "http://www.wikidata.org/entity/Q9200010"}}]
    fake.data["sparql"]["season_check:" + SERIES] = {"results": {"bindings": bindings}}
    return fake


def fetch(fake: FakeWikimedia, *, seasons_total: int | None = 9, **kw: Any) -> dict[str, Any]:
    client = HttpClient(fake, None, sleep=lambda s: None, min_interval={})
    cand = {"qid": SERIES, "enwiki_title": MAIN, "media_type": "tv_series", "title": "Tidewater",
            "year": 1989, "tmdb_id": 92000, "series_status": "ended",
            "series_status_basis": "P582", "number_of_seasons": seasons_total}
    return PlotFetcher(client, clock=lambda: NOW, **kw).fetch(cand)


def page_requests(fake: FakeWikimedia) -> list[str]:
    """oldids of whole-page parse requests (episode tables), in order."""
    out = []
    for r in fake.requests:
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.url).query))
        if params.get("action") == "parse" and params.get("prop") == "text" and (
                "section" not in params):
            out.append(params["oldid"])
    return out


TWO_SEASONS = {1: season_page_html(1, 3, 40), 2: season_page_html(2, 3, 40),
               4: season_page_html(4, 3, 40)}


# --- parsing real episode tables (cached English Wikipedia HTML) ----------------------------


def test_real_season_page_table_takes_only_summary_prose() -> None:
    """Twin Peaks season 3: two number cells, title cell with an alternative title, director,
    writer, release date and viewers (with a reference): only the description and a marker."""
    tables = parse_tables(fixture("twin_peaks_season_3_episodes"))
    assert [t.heading for t in tables] == ["episodes"]
    used, skipped = season_page_tables(tables)
    se = season_episodes(3, used)
    assert skipped == [] and se.rows == 3 and se.without_summary == 0
    assert [e.number for e in se.episodes] == [1, 2, 3]
    assert se.episodes[0].title == '"Part 1" "My Log Has a Message for You"'
    first = se.episodes[0].paragraph
    assert first.startswith('S3E1 "Part 1" "My Log Has a Message for You": In 2014, 25 years')
    text = "\n\n".join(e.paragraph for e in se.episodes)
    for cell in ("David Lynch", "Mark Frost", "May 21, 2017", "0.506", "2017-05-21",
                 "Porter, Rick", "See also", "[1]"):
        assert cell not in text, cell
    assert word_count(text) == sum(e.words for e in se.episodes)


def test_real_tables_under_season_headings_with_a_continuation_row() -> None:
    """Shrinking: tables under "Season N (year)" headings, as on an episode-list page. Season 3
    opens with a two-part row (rowspan="2" cells and continuation rows holding only a second
    production code); it is one episode, "My Bad", not three. Episodes without a summary
    (not yet aired) are counted, not used."""
    seasons, skipped = list_page_seasons(parse_tables(fixture("shrinking_seasons_episodes")))
    assert skipped == []
    assert [(n, len(ts)) for n, ts in seasons] == [(1, 1), (2, 1), (3, 1)]
    s3 = season_episodes(3, seasons[2][1])
    assert s3.rows == 11 and s3.without_summary == 9
    assert [(e.number, e.title) for e in s3.episodes] == [(1, '"My Bad"'),
                                                         (2, '"Happiness Mission"')]
    s1 = season_episodes(1, seasons[0][1])
    assert s1.episodes[0].paragraph.startswith('S1E1 "Coin Flip": Jimmy Laird, a therapist')
    assert "James Ponsoldt" not in s1.episodes[0].paragraph  # the director cell
    assert "T12.17451" not in s1.episodes[0].paragraph  # the production code cell


def test_real_fixtures_are_recorded_with_licence_and_permalink() -> None:
    sources = json.loads((FIXTURES / "sources.json").read_text(encoding="utf-8"))
    assert set(sources) == {p.stem for p in FIXTURES.glob("*.html")}
    for meta in sources.values():
        assert meta["license"] == "CC-BY-SA-4.0"
        assert meta["permalink"].startswith("https://en.wikipedia.org/w/index.php?title=")
        assert f"oldid={meta['revision']}" in meta["permalink"]


def test_hand_built_tables_take_only_summary_cells_and_strip_cite_errors() -> None:
    html = season_page_html(1, 2, 20, cite_error=True)
    used, skipped = season_page_tables(parse_tables(html))
    assert skipped == [{"heading": "specials", "reason": "specials or extras, out of the "
                        "season's order"}]
    se = season_episodes(1, used)
    text = "\n\n".join(e.paragraph for e in se.episodes)
    assert text.split("\n\n")[0] == 'S1E1 "Episode 1-1": ' + words(20, "s1e1w")
    for cell in (*NON_SUMMARY, "Cite error", "premiered", "Ann Actor", "Ratings source",
                 "List of Tidewater", "[2]"):
        assert cell not in text, cell


def test_episode_without_title_gets_a_bare_marker_and_single_number_uses_position() -> None:
    rows = ep_rows(2, 2, 10, titles=False).replace('<td style="text-align:center">1</td>', "")
    rows = rows.replace('<td style="text-align:center">2</td>', "")  # only the overall number
    se = season_episodes(2, parse_tables(table(rows)))
    assert [e.paragraph.split(":")[0] for e in se.episodes] == ["S2E1", "S2E2"]


# --- list-page grouping ---------------------------------------------------------------------


def test_list_page_seasons_rise_and_a_restart_stops_the_page() -> None:
    html = list_page_html({1: (2, 20), 2: (2, 20), 3: (1, 20)}, restart=True)
    seasons, skipped = list_page_seasons(parse_tables(html))
    assert [n for n, _ in seasons] == [1, 2, 3]
    reasons = [(s["heading"], s["reason"].split(":")[0]) for s in skipped]
    assert reasons == [("specials", "not under a season heading"),
                       ("series 1 (2005)", "season 1 after season 3")]


# --- the join: order and the cap at an episode boundary ------------------------------------


def _season(n: int, sizes: list[int]) -> SeasonEpisodes:
    from laminary_pipeline.ingest.episodes import Episode

    eps = [Episode(n, i + 1, f'"E{i + 1}"', words(s, f"s{n}e{i}w"), i + 1)
           for i, s in enumerate(sizes)]
    return SeasonEpisodes(n, eps, len(eps))


def test_join_stops_at_the_first_episode_over_the_cap() -> None:
    s1, s2, s3 = _season(1, [50, 50]), _season(2, [50, 50, 50]), _season(3, [10])
    sizes = [e.words for s in (s1, s2, s3) for e in s.episodes]  # marker words included
    cap = sum(sizes[:3]) + sizes[3] - 1  # season 2's second episode doesn't fit
    joined = join_episodes([s1, s2, s3], cap, 20)
    assert [(s.season, len(s.episodes)) for s in joined.seasons] == [(1, 2), (2, 1)]
    assert joined.partial_season == 2 and joined.stopped_by == "word_cap"
    assert joined.left_out_episodes == 3 and joined.left_out_seasons == [3]
    assert joined.words == sum(sizes[:3]) <= cap
    # a later, smaller episode never jumps the queue: season 3's 10-word episode is left out
    exact = join_episodes([s1, s2], sum(sizes[:5]), 20)
    assert exact.partial_season is None and exact.left_out_episodes == 0


def test_join_respects_the_source_limit() -> None:
    seasons = [_season(n, [5]) for n in range(1, 24)]
    joined = join_episodes(seasons, 10_000, 20)
    assert len(joined.seasons) == 20 and joined.stopped_by == "source_limit"
    assert joined.left_out_seasons == [21, 22, 23] and joined.partial_season is None


# --- the fetcher: when the fallback runs, and what it writes --------------------------------


def test_series_with_failing_prose_uses_episode_tables_in_order() -> None:
    fake = episode_fake(season_pages=TWO_SEASONS)
    rec = fetch(fake)
    assert rec["status"] == "ok" and rec["fetcher_version"] == FETCHER_VERSION
    assert rec["via"] == "season_articles" and rec["via_detail"] == "episode_table"
    assert rec["section"] == {"heading": "episode tables", "index": None}
    assert rec["main_article"]["skip_reason"] == "too_short"
    parts = rec["sources"]
    assert [(p["page_title"], p["source"]["season"]) for p in parts] == [
        ("Tidewater season 1", 1), ("Tidewater season 2", 2), ("Tidewater season 4", 4)]
    assert all(p["via_detail"] == "episode_table" for p in parts)
    assert parts[0]["episodes"] == ["S1E1", "S1E2", "S1E3"]
    paragraphs = parts[0]["text"].split("\n\n")
    assert [p.split(":")[0] for p in paragraphs] == [
        'S1E1 "Episode 1-1"', 'S1E2 "Episode 1-2"', 'S1E3 "Episode 1-3"']
    assert paragraphs[1] == 'S1E2 "Episode 1-2": ' + words(40, "s1e2w")
    for p in parts:
        src = p["source"]
        assert src["content_sha256"] == sha256_text(p["text"])
        assert src["word_count"] == word_count(p["text"])
        assert src["ref"] == f"https://en.wikipedia.org/wiki/Tidewater_season_{src['season']}"
        assert "oldid=" in p["permalink"]
        for cell in NON_SUMMARY:
            assert cell not in p["text"]
    assert rec["word_count"] == sum(p["source"]["word_count"] for p in parts)
    report = rec["episode_tables"]
    assert report["page_kind"] == "season_pages"
    assert report["pages_used"] == ["Tidewater season 1", "Tidewater season 2",
                                    "Tidewater season 4"]
    assert [e["ordinal"] for e in report["evidence"]] == [1, 2, 4]
    assert all(e["property"] == "P179" and e["series"] == SERIES for e in report["evidence"])
    assert report["episodes_used"] == 9 and report["partial_season"] is None
    assert report["left_out_over_cap"] == {"episodes": 0, "seasons": []}
    assert {t["heading"] for t in report["tables_skipped"]} == {"specials"}
    assert rec["coverage"] == {"seasons": [1, 2, 4], "total_seasons": 9,
                               "total_seasons_basis": "wikidata_P2437", "partial": True}
    assert rec["season_articles"]["used"] == []  # the prose attempt is kept


def test_cap_stops_at_an_episode_boundary_and_later_pages_are_not_fetched() -> None:
    fake = episode_fake(season_pages=TWO_SEASONS)
    full = fetch(episode_fake(season_pages=TWO_SEASONS))
    s1 = full["sources"][0]["source"]["word_count"]
    per_episode = word_count(full["sources"][1]["text"].split("\n\n")[0])
    cap = s1 + per_episode + 5  # season 2's first episode fits, its second doesn't
    rec = fetch(fake, season_word_cap=cap)
    assert rec["status"] == "ok"
    assert [len(p["episodes"]) for p in rec["sources"]] == [3, 1]
    assert rec["word_count"] == s1 + per_episode <= cap
    report = rec["episode_tables"]
    assert report["partial_season"] == 2 and report["stopped_by"] == "word_cap"
    assert report["by_season"] == [{"season": 1, "episodes": 3, "with_summary": 3},
                                   {"season": 2, "episodes": 1, "with_summary": 3}]
    assert report["left_out_over_cap"] == {"episodes": 2, "seasons": []}
    assert report["pages_not_fetched"] == ["Tidewater season 4"]
    assert page_requests(fake) == ["92010", "92020"]  # season 4 never fetched
    assert rec["coverage"]["seasons"] == [1, 2] and rec["coverage"]["partial_season"] == 2


def test_list_page_is_used_when_no_season_page_has_summaries() -> None:
    empty = season_page_html(1, 3, 40, summaries=False)
    html = list_page_html({1: (3, 40), 2: (2, 40), 3: (2, 40)}, restart=True)
    fake = episode_fake(season_pages={1: empty}, list_html=html)
    rec = fetch(fake, seasons_total=None)
    assert rec["status"] == "ok" and rec["via_detail"] == "episode_table"
    parts = rec["sources"]
    assert [p["source"]["season"] for p in parts] == [1, 2, 3]  # one source per season
    assert {p["page_title"] for p in parts} == {"List of Tidewater episodes"}
    assert {p["source"]["ref"] for p in parts} == {
        "https://en.wikipedia.org/wiki/List_of_Tidewater_episodes"}
    report = rec["episode_tables"]
    assert report["page_kind"] == "episode_list_page"
    assert report["pages_used"] == ["List of Tidewater episodes"]
    assert report["evidence"][0]["property"] == "P361"
    assert report["pages_skipped"] == [{"title": "Tidewater season 1",
                                        "reason": "no episode summaries in its episode tables"}]
    # no Wikidata total: the highest season among verified pages and the list page's headings
    assert rec["coverage"]["total_seasons"] == 3
    assert rec["coverage"]["total_seasons_basis"] == "verified_season_pages"
    assert "s9e1w0" not in json.dumps(parts)  # the specials table under "Specials"
    assert "Episode 1-1" in parts[0]["text"] and parts[0]["text"].count("S1E") == 3


def test_too_little_episode_text_stays_too_short_with_the_report() -> None:
    fake = episode_fake(season_pages={1: season_page_html(1, 2, 30)})
    rec = fetch(fake)
    assert rec["status"] == "skipped" and rec["skip_reason"] == "too_short"
    assert "episode tables: 2 episodes with" in rec["skip_detail"]
    assert rec["episode_tables"]["episodes_used"] == 2
    assert rec["fetcher_version"] == FETCHER_VERSION


def test_no_pages_at_all_still_records_the_attempt() -> None:
    rec = fetch(episode_fake())
    assert rec["skip_reason"] == "too_short"
    assert rec["episode_tables"]["page_kind"] is None
    assert rec["episode_tables"]["episodes_used"] == 0


def test_episode_tables_are_not_tried_when_season_prose_works() -> None:
    fake = episode_fake(season_pages=TWO_SEASONS, prose_words=200)
    rec = fetch(fake)
    assert rec["status"] == "ok" and "via_detail" not in rec and "episode_tables" not in rec
    assert page_requests(fake) == []


def test_episode_tables_are_tried_after_season_too_long() -> None:
    """QA S2 (DECISIONS 2026-10-02): a season article over the 6,000-word ceiling no longer
    ends the series: its episode tables are tried."""
    fake = episode_fake(season_pages=TWO_SEASONS, prose_words=6001)
    rec = fetch(fake)
    assert rec["status"] == "ok" and rec["via_detail"] == "episode_table"
    assert rec["season_articles"]["used"] == []
    assert page_requests(fake) == ["92010", "92020", "92040"]


def test_season_too_long_stays_when_episode_tables_fail() -> None:
    fake = episode_fake(season_pages={1: season_page_html(1, 2, 30)}, prose_words=6001)
    rec = fetch(fake)
    assert rec["skip_reason"] == "season_too_long"
    assert "episode tables: 2 episodes with" in rec["skip_detail"]
    assert rec["episode_tables"]["episodes_used"] == 2


def test_episode_tables_are_not_tried_when_season_lookup_is_off() -> None:
    fake = episode_fake(season_pages=TWO_SEASONS)
    rec = fetch(fake, season_articles=False)
    assert rec["season_articles"] == {"status": "disabled"} and page_requests(fake) == []


def test_only_english_wikipedia_and_wikidata_are_contacted() -> None:
    fake = episode_fake(season_pages=TWO_SEASONS)
    fetch(fake)
    assert fake.hosts == {"en.wikipedia.org", "query.wikidata.org"}


# --- annotation input: gate, coverage line, verify_request ----------------------------------


def _plot(rec: dict[str, Any]) -> dict[str, Any]:
    assert rec["status"] == "ok"
    return rec


@pytest.fixture
def episode_plot() -> dict[str, Any]:
    return _plot(fetch(episode_fake(season_pages=TWO_SEASONS)))


@pytest.fixture
def partial_plot() -> dict[str, Any]:
    full = fetch(episode_fake(season_pages=TWO_SEASONS))
    s1 = full["sources"][0]["source"]["word_count"]
    per_episode = word_count(full["sources"][1]["text"].split("\n\n")[0])
    return _plot(fetch(episode_fake(season_pages=TWO_SEASONS),
                       season_word_cap=s1 + per_episode + 5))


def _request(plot_obj: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    return build_request(parse_plot(plot_obj, "t"), model="claude-opus-5-5",
                         prompt=load_prompt(DEFAULT_PROMPT_VERSION), effort="medium",
                         max_tokens=16000)


def test_episode_plot_is_valid_input_and_sends_the_coverage_line(episode_plot) -> None:
    plot = parse_plot(episode_plot, "t")
    assert plot.via_detail == "episode_table" and plot.partial_season is None
    _, params = _request(episode_plot)
    content = params["messages"][0]["content"]
    assert content[0]["text"].splitlines()[2] == "Summary covers seasons 1–2 and 4 of 9."
    assert [content[i]["text"] for i in source_block_indices(3)] == [
        p["text"] for p in episode_plot["sources"]]


def test_partial_season_line_is_built_from_validated_ints(partial_plot) -> None:
    assert partial_plot["coverage"]["partial_season"] == 2
    gated, params = _request(partial_plot)
    line = params["messages"][0]["content"][0]["text"].splitlines()[2]
    assert line == f"Summary covers seasons 1–2 of 9 (season 2 only in part{TAIL}"
    assert prompt_coverage(gated.plot) == {
        "seasons": [1, 2], "total_seasons": 9, "total_seasons_basis": "wikidata_P2437",
        "statement": line}


@pytest.mark.parametrize(
    ("seasons", "total", "partial", "line"),
    [
        ([1], 9, 1, f"Summary covers season 1 of 9 (season 1 only in part{TAIL}"),
        ([1, 2], 2, 2, f"Summary covers seasons 1–2 of 2 (season 2 only in part{TAIL}"),
        ([2, 3], 5, 3, f"Summary covers seasons 2–3 of 5 (season 3 only in part{TAIL}"),
        ([1, 2], 2, None, None),  # every season, in full: no line
    ],
)
def test_coverage_line_for_partial_seasons(seasons: list[int], total: int,
                                           partial: int | None, line: str | None) -> None:
    plot = _synthetic(seasons, total=total, partial=partial)
    gated = gate(parse_plot(plot, "t"))
    coverage = prompt_coverage(gated.plot)
    assert (coverage["statement"] if coverage else None) == line
    if line:
        assert len(line) <= 200  # the schema's statement maxLength


def test_changed_partial_line_is_caught_by_verify_request(partial_plot) -> None:
    prompt = load_prompt(DEFAULT_PROMPT_VERSION)
    gated, params = _request(partial_plot)
    for old, new in ((" (season 2 only in part; the summary stops before that season ends)",
                      ""), ("; the summary stops before that season ends", ""),
                     ("season 2 only", "season 1 only"),
                     ("of 9", "of 2")):
        bad = copy.deepcopy(params)
        bad["messages"][0]["content"][0]["text"] = (
            bad["messages"][0]["content"][0]["text"].replace(old, new))
        with pytest.raises(GateError, match="block 0"):
            verify_request(bad, gated, prompt)


def _synthetic(
    seasons: list[int], *, total: int = 9, partial: int | None = None, size: int = 80,
    fetcher: str = "1.5.0", via_detail: str | None = "episode_table",
    season_on_sources: bool = True,
) -> dict[str, Any]:
    sources = []
    for n in seasons:
        text = "\n\n".join(f'S{n}E{i} "E{i}": ' + words(size, f"s{n}e{i}w") for i in (1, 2))
        src = {"kind": "wikipedia_plot",
               "ref": "https://en.wikipedia.org/wiki/List_of_Tidewater_episodes",
               "revision": "92100", "retrieved_at": NOW, "license": "CC-BY-SA-4.0",
               "word_count": word_count(text), "content_sha256": sha256_text(text)}
        if season_on_sources:
            src["season"] = n
        sources.append({"page_title": "List of Tidewater episodes", "permalink": "p",
                        "via_detail": "episode_table", "source": src, "text": text})
    coverage: dict[str, Any] = {"seasons": seasons if season_on_sources else None,
                                "total_seasons": total, "total_seasons_basis": "wikidata_P2437"}
    if not season_on_sources:
        del coverage["seasons"]
    if partial is not None:
        coverage["partial_season"] = partial
    plot: dict[str, Any] = {
        "fetcher_version": fetcher, "qid": SERIES, "status": "ok", "via": "season_articles",
        "candidate": {"title": "Tidewater", "year": 1989, "media_type": "tv_series",
                      "tmdb_id": 92000, "series_status": "ended", "series_status_basis": "P582"},
        "sources": sources, "coverage": coverage,
        "word_count": sum(s["source"]["word_count"] for s in sources),
    }
    if via_detail is not None:
        plot["via_detail"] = via_detail
    return plot


def test_gate_holds_episode_table_input_to_the_cap() -> None:
    """Ingest never produces more than the ~3,000-word cap from episode tables, so the gate
    accepts up to it and refuses anything larger (no 6,000-word lead block)."""
    at_cap = _synthetic([1], size=1498)  # 2 x (1498 + 2 marker words) = 3,000
    assert sum(s["source"]["word_count"] for s in at_cap["sources"]) <= 3000
    gate(parse_plot(at_cap, "t"))
    exact = _synthetic([1, 2], size=748)  # 4 x (748 + 2) = 3,000
    assert exact["word_count"] == 3000
    gate(parse_plot(exact, "t"))
    over = _synthetic([1, 2], size=749)  # 3,004
    with pytest.raises(GateError, match="over the 3000-word episode-table cap"):
        gate(parse_plot(over, "t"))
    # the same text without via_detail is ordinary season-article input: the ceiling applies
    gate(parse_plot(_synthetic([1, 2], size=749, via_detail=None), "t"))


def test_gate_refuses_episode_table_input_without_seasons() -> None:
    plot = _synthetic([1], season_on_sources=False)
    with pytest.raises(GateError, match="a season on every source"):
        gate(parse_plot(plot, "t"))


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ("detail", "via_detail 'episode_tables' is not"),
        ("no_via", "without via"),
        ("old_fetcher", "from fetcher '1.4.0'"),
        ("partial_not_last", "not the last source's season"),
        ("partial_bool", "not the last source's season"),
        ("partial_without_episodes", "partial_season without episode-table"),
    ],
)
def test_malformed_episode_table_files_are_refused(change: str, match: str) -> None:
    plot = _synthetic([1, 2], partial=2)
    if change == "detail":
        plot["via_detail"] = "episode_tables"
    elif change == "no_via":  # the generic input shape, without via
        plot = {"title": {"media_type": "tv_series", "name": "Tidewater", "release_year": 1989,
                          "tmdb_id": 92000, "series_status": "ended"},
                "sources": [{**p["source"], "text": p["text"]} for p in plot["sources"]],
                "via_detail": "episode_table"}
    elif change == "old_fetcher":
        plot["fetcher_version"] = "1.4.0"
    elif change == "partial_not_last":
        plot["coverage"]["partial_season"] = 1
    elif change == "partial_bool":
        plot["coverage"]["partial_season"] = True
    else:
        del plot["via_detail"]
    with pytest.raises(InputFormatError, match=match):
        parse_plot(plot, "t")


def test_annotate_1_2_0_accepts_fetcher_1_5_files(episode_plot) -> None:
    assert episode_plot["fetcher_version"] == FETCHER_VERSION
    _request(episode_plot)  # require_current_ingest: 1.5.x >= 1.4.0


def test_gate_accepts_every_shape_the_fetcher_produces() -> None:
    """Over a range of caps, each fetched record passes the gate and the request checks, and
    never exceeds the cap."""
    for cap in (160, 300, 400, 455, 600, 2000):
        rec = fetch(episode_fake(season_pages=TWO_SEASONS), season_word_cap=cap)
        if rec["status"] != "ok":
            continue
        assert rec["word_count"] <= cap
        _request(rec)


# --- records and gold -----------------------------------------------------------------------


def test_record_stores_the_partial_coverage_statement(partial_plot) -> None:
    from annotate_support import response, valid_output

    from laminary_pipeline.annotate.client import Usage
    from laminary_pipeline.annotate.records import Invalid, RunContext, interpret

    gated = gate(parse_plot(partial_plot, "t"))
    ctx = RunContext(run_id="run_test", model="claude-opus-5-5",
                     prompt_version=DEFAULT_PROMPT_VERSION, effort="medium", batch=False)
    rec = interpret(response(valid_output("breaking_bad")), gated, ctx,
                    usage_so_far=Usage(1000, 2000, 5000, 0), attempts=1)
    assert not isinstance(rec, Invalid), rec
    assert rec["provenance"]["coverage"]["statement"] == (
        f"Summary covers seasons 1–2 of 9 (season 2 only in part{TAIL}")
    assert [s["season"] for s in rec["provenance"]["sources"]] == [1, 2]
    assert validate_record(rec) == []


def test_gold_text_equals_the_joined_model_input(partial_plot) -> None:
    prompt = load_prompt(DEFAULT_PROMPT_VERSION)
    gated, params = _request(partial_plot)
    blocks = [b["text"] for b in params["messages"][0]["content"]]
    n = len(partial_plot["sources"])
    joined = "\n\n".join("".join(blocks[1 + 3 * i: 4 + 3 * i]) for i in range(n))
    assert labeler_text(gated) == joined
    assert labeler_coverage(gated) == (
        f"Summary covers seasons 1–2 of 9 (season 2 only in part{TAIL}")
    verify_request(params, gated, prompt)

    selection = [{"qid": SERIES, "title": "Tidewater", "year": 1989, "media_type": "tv_series",
                  "tmdb_id": 92000}]
    rows, skipped = template_rows(selection, {SERIES: partial_plot})
    assert skipped == []
    assert rows[0]["plot_section"] == "Episode tables (2 seasons)"
    assert rows[0]["summary_coverage"] == labeler_coverage(gated)
    assert rows[0]["source_sha256"] == " | ".join(
        p["source"]["content_sha256"] for p in partial_plot["sources"])
    manifest = manifest_rows(rows, {SERIES: partial_plot})
    assert manifest[0]["sha256"] == hashlib.sha256(joined.encode("utf-8")).hexdigest()
    parsed = list(csv.DictReader(io.StringIO(template_csv(rows))))
    assert parsed[-1]["summary_coverage"] == labeler_coverage(gated)


def test_single_season_gold_text_is_the_source_text() -> None:
    plot = _synthetic([1], partial=1)
    gated = gate(parse_plot(plot, "t"))
    assert labeler_text(gated) == plot["sources"][0]["text"]
    assert sha256_text(labeler_text(gated)) == plot["sources"][0]["source"]["content_sha256"]


# --- plots report: episode tables, priority series, backfill --------------------------------


def _cands(rows: list[tuple[str, str, str, int]]) -> list[dict[str, Any]]:
    return [{"qid": q, "title": t, "media_type": "tv_series", "region": "english",
             "language": "english", "decade": "2000s", "bucket": "tv:english", "role": role,
             "bucket_rank": rank} for q, t, role, rank in rows]


def _skipped(qid: str, reason: str = "too_short") -> dict[str, Any]:
    return {"qid": qid, "status": "skipped", "skip_reason": reason,
            "skip_detail": "longest plot-like section 'premise' has 40 words; episode tables: "
            "0 episodes with 0 words", "fetcher_version": "1.5.0",
            "candidate": {"media_type": "tv_series"}, "season_articles": {"used": []},
            "episode_tables": {"episodes_used": 0}}


def test_report_counts_via_episode_tables_per_bucket(tmp_path: Path, episode_plot) -> None:
    paths = DataPaths.resolve(str(tmp_path))
    write_json_atomic(paths.plot_file(SERIES), episode_plot)
    report = plots_report(paths, _cands([(SERIES, "Tidewater", "pilot", 1)]))
    assert report["via_season_articles"] == {"total": 1, "by_bucket": {"tv:english": 1}}
    assert report["via_episode_tables"] == {"total": 1, "by_bucket": {"tv:english": 1}}
    text = format_report(report)
    assert "Via episode tables (of those): 1 passing titles" in text
    assert "episode tables" in report["sections_used"]


def test_priority_report_lists_skipped_series_with_the_reason(tmp_path: Path,
                                                              episode_plot) -> None:
    assert set(PRIORITY_SERIES) == {"Q23733", "Q16290", "Q494244", "Q751917", "Q23572",
                                    "Q192837", "Q4525", "Q485668", "Q252118", "Q34316"}
    paths = DataPaths.resolve(str(tmp_path))
    write_json_atomic(paths.plot_file("Q23572"), {**episode_plot, "qid": "Q23572"})
    write_json_atomic(paths.plot_file("Q23733"), _skipped("Q23733"))
    write_json_atomic(paths.plot_file("Q485668"), {**episode_plot, "qid": "Q485668"})
    cands = _cands([("Q23572", "Game of Thrones", "pilot", 1),
                    ("Q23733", "Seinfeld", "pilot", 2),
                    ("Q4525", "NCIS", "reserve", 1),
                    ("Q485668", "Scrubs", "reserve", 2)])
    report = plots_report(paths, cands)
    by_qid = {e["qid"]: e for e in report["priority_series"]}
    assert len(by_qid) == 10
    assert by_qid["Q23572"]["status"] == "pass"
    assert by_qid["Q23572"]["via_detail"] == "episode_table"
    assert by_qid["Q23733"]["status"] == "skipped"
    assert by_qid["Q23733"]["skip_reason"] == "too_short"
    assert by_qid["Q4525"]["status"] == "not_fetched"
    # passes the plot rules, but a reserve outside the effective pilot: not a pass (QA B1)
    assert by_qid["Q485668"]["status"] == "reserve"
    assert by_qid["Q34316"]["status"] == "not_a_candidate"
    text = format_report(report)
    assert "Priority series (DECISIONS 2026-10-02): 1/10 pass (in the effective pilot)" in text
    assert ("WARNING for Hari: Q23733 'Seinfeld' (tv:english, role pilot) skipped: too_short: "
            "longest plot-like section") in text
    assert "WARNING for Hari: Q4525 'NCIS' (tv:english, role reserve) is not fetched" in text
    assert ("WARNING for Hari: Q485668 'Scrubs' (tv:english, role reserve) passes but is a "
            "reserve, not in the effective pilot") in text
    assert "WARNING for Hari: Q34316 'Doctor Who' is not a candidate" in text


class _CountingFetcher:
    """Stands in for PlotFetcher in backfill: records which titles it was asked to fetch."""

    def __init__(self) -> None:
        self.fetched: list[str] = []

    def fetch(self, cand: dict[str, Any]) -> dict[str, Any]:
        self.fetched.append(cand["qid"])
        return _annotatable_ok(cand["qid"])


def _annotatable_ok(qid: str) -> dict[str, Any]:
    """A passing plot file the annotation gate accepts (fills a pilot slot, QA 2026-10-02)."""
    from annotate_support import ingest_plot

    return {**ingest_plot(qid, tmdb_id=int(qid[1:])), "word_count": 200,
            "section": {"heading": "plot"}}


def test_backfill_never_replaces_a_priority_series(tmp_path: Path) -> None:
    paths = DataPaths.resolve(str(tmp_path))
    write_json_atomic(paths.plot_file("Q23733"), _skipped("Q23733"))
    write_json_atomic(paths.plot_file("Q91"), {"qid": "Q91", "status": "skipped",
                                               "skip_reason": "too_short",
                                               "fetcher_version": "1.5.0",
                                               "candidate": {"media_type": "movie"}})
    write_json_atomic(paths.plot_file("Q90"), _annotatable_ok("Q90"))
    cands = _cands([("Q90", "Passing", "pilot", 1), ("Q23733", "Seinfeld", "pilot", 2),
                    ("Q91", "Thin", "pilot", 3), ("Q80", "Reserve A", "reserve", 1),
                    ("Q81", "Reserve B", "reserve", 2)])
    logs: list[str] = []
    fetcher = _CountingFetcher()
    run = run_plots(paths, cands, fetcher, backfill=True, log=logs.append)  # type: ignore[arg-type]
    # one reserve replaces the thin ordinary title; none replaces Seinfeld
    assert fetcher.fetched == ["Q80"] and run.backfilled == 1
    warning = [m for m in logs if m.startswith("WARNING for Hari: priority series Q23733")]
    assert warning and "Its pilot slot is held, not backfilled." in warning[0]
    # the effective pilot leaves Seinfeld's slot empty even with a passing reserve available
    write_json_atomic(paths.plot_file("Q81"), _annotatable_ok("Q81"))
    assert [r["qid"] for r in effective_pilot(paths, cands)] == ["Q90", "Q80"]
    report = plots_report(paths, cands)
    assert report["effective_pilot"]["held_for_priority_series"] == ["Q23733"]
    assert report["effective_pilot"]["by_bucket"]["tv:english"] == "2/3"
    assert "slots held for skipped priority series (not backfilled): Q23733" in (
        format_report(report))


def test_priority_series_skipped_by_an_older_fetcher_are_fetched_again(tmp_path: Path) -> None:
    from laminary_pipeline.ingest.plots import needs_fetch

    paths = DataPaths.resolve(str(tmp_path))
    old = {**_skipped("Q23733"), "fetcher_version": "1.4.0"}
    del old["episode_tables"]
    write_json_atomic(paths.plot_file("Q23733"), old)
    assert needs_fetch(paths, "Q23733", False)
    write_json_atomic(paths.plot_file("Q23733"), _skipped("Q23733"))
    assert not needs_fetch(paths, "Q23733", False)


# --- episode-list pages split by year range, season range or part ---------------------------


@pytest.mark.parametrize(
    ("title", "key"),
    [
        ("List of CID episodes", (0, 0)),
        ("List of CID episodes: 1998–2009", (1, 1998)),
        ("List of CID episodes: 2024–present", (1, 2024)),
        ("List of CID episodes: 2010-2014", (1, 2010)),
        ("List of CID episodes (1998–2009)", (1, 1998)),
        ("List of CID episodes (seasons 1–5)", (2, 1)),
        ("List of CID episodes (series 6–10)", (2, 6)),
        ("List of CID episodes (part 2)", (3, 2)),
        ("List of CID (Indian TV series) episodes: 1998–2009", (1, 1998)),
        ("list of CID episodes: 1998–2009", (1, 1998)),  # only the first letter is free
    ],
)
def test_split_episode_list_names_are_recognised(title: str, key: tuple[int, int]) -> None:
    from laminary_pipeline.ingest.seasons import episode_list_order, is_episode_list

    assert episode_list_order(title, "CID", "CID (Indian TV series)") == key
    assert is_episode_list(title, "CID", "CID (Indian TV series)")


@pytest.mark.parametrize("title", [
    "List of CID episodes: highlights", "List of CID episodes (1998)",
    "List of CID characters", "List of CID Special Bureau episodes",
    "List of CID episodes (seasons 1–5) extra", "List of CID episodes: 98–09",
    "CID episodes: 1998–2009", "List of Cid episodes", "list of cid episodes: 1998–2009",
])
def test_other_titles_are_not_episode_lists(title: str) -> None:
    from laminary_pipeline.ingest.seasons import is_episode_list

    assert not is_episode_list(title, "CID", "CID (Indian TV series)")


SPLIT_LISTS = {  # title -> (pageid, revid, Wikidata item, seasons on the page)
    "List of Tidewater episodes: 2010–present": (9230, 92300, "Q9200031", {3: (2, 40)}),
    "List of Tidewater episodes: 1998–2009": (9220, 92200, "Q9200030", {1: (2, 40),
                                                                         2: (2, 40)}),
}


def split_list_fake(*, verified: tuple[str, ...] = ("Q9200030", "Q9200031")) -> FakeWikimedia:
    """No season pages; the main article links two year-range list pages (newest first)."""
    fake = episode_fake()
    w = fake.data["wikipedia"]
    w["links:92000"] = {"parse": {"links": [
        {"ns": 0, "title": t, "exists": True} for t in SPLIT_LISTS]}}
    for title, (pid, rev, item, seasons) in SPLIT_LISTS.items():
        w[f"query:{title}"] = page(title, pid, rev, item)
        w[f"sections:{rev}"] = sections(title, ["Series overview"])
        w[f"text:{rev}:1"] = {"parse": {"text": "<div><table><tr><td>1</td></tr></table></div>"}}
        w[f"page:{rev}"] = {"parse": {"text": list_page_html(seasons)}}
        # each list page links back to the main article (fetcher 1.5.3 link fallback)
        w[f"links:{rev}:0"] = {"parse": {"links": [{"ns": 0, "title": MAIN, "exists": True}]}}
    fake.data["sparql"]["season_check:" + SERIES] = {"results": {"bindings": [
        {"item": {"value": f"http://www.wikidata.org/entity/{q}"}} for q in verified]}}
    return fake


def test_split_list_pages_are_verified_ordered_by_year_and_joined() -> None:
    fake = split_list_fake()
    rec = fetch(fake, seasons_total=None)
    assert rec["status"] == "ok" and rec["via_detail"] == "episode_table"
    assert [(p["page_title"], p["source"]["season"]) for p in rec["sources"]] == [
        ("List of Tidewater episodes: 1998–2009", 1),
        ("List of Tidewater episodes: 1998–2009", 2),
        ("List of Tidewater episodes: 2010–present", 3)]
    report = rec["episode_tables"]
    assert report["page_kind"] == "episode_list_page"
    assert report["pages_used"] == ["List of Tidewater episodes: 1998–2009",
                                    "List of Tidewater episodes: 2010–present"]
    assert [e["item"] for e in report["evidence"]] == ["Q9200030", "Q9200031"]
    assert page_requests(fake) == ["92200", "92300"]  # earlier years first
    assert rec["coverage"]["total_seasons"] == 3


def test_unverified_split_list_page_is_skipped() -> None:
    """A split page without the P179/P361 statement that Wikidata places in another series is
    not used, even though the main article links to it (fetcher 1.5.2)."""
    fake = split_list_fake(verified=("Q9200030",))
    fake.data["sparql"]["list_statements"] = {"results": {"bindings": [
        {"item": {"value": "http://www.wikidata.org/entity/Q9200031"},
         "prop": {"value": "P179"}, "value": {"value": "http://www.wikidata.org/entity/Q77"},
         "tv": {"value": "true"}}]}}
    rec = fetch(fake, seasons_total=None)
    assert [p["source"]["season"] for p in rec["sources"]] == [1, 2]
    reasons = {s["title"]: s["reason"] for s in rec["season_articles"]["skipped"]}
    assert reasons["List of Tidewater episodes: 2010–present"] == (
        "unverified: Wikidata places item Q9200031 in another television series (P179 Q77), "
        "not Q9200000")


def test_split_list_pages_must_keep_seasons_rising() -> None:
    fake = split_list_fake()
    rev = SPLIT_LISTS["List of Tidewater episodes: 2010–present"][1]
    fake.data["wikipedia"][f"page:{rev}"] = {"parse": {"text": list_page_html(
        {2: (2, 40), 3: (2, 40)})}}
    rec = fetch(fake, seasons_total=None)
    assert [p["source"]["season"] for p in rec["sources"]] == [1, 2, 3]
    skipped = rec["episode_tables"]["tables_skipped"]
    assert {"title": "List of Tidewater episodes: 2010–present", "heading": "season 2",
            "reason": "season 2 after season 2 on an earlier page"} in skipped


# --- QA round 2 ------------------------------------------------------------------------------


def test_missing_summary_tail_marks_the_last_season_partial() -> None:
    """QA S1: Shrinking season 3 has 11 episode rows but summaries for only the first 2, so
    the summary stops before that season ends even though the cap was not reached."""
    seasons, _ = list_page_seasons(parse_tables(fixture("shrinking_seasons_episodes")))
    joined = join_episodes([season_episodes(n, ts) for n, ts in seasons], 3000, 20)
    assert [(s.season, len(s.episodes)) for s in joined.seasons] == [(1, 2), (2, 2), (3, 2)]
    assert joined.partial_season == 3 and joined.partial_reason == "missing_summaries"
    assert joined.stopped_by is None
    # a season whose rows all have summaries is not partial
    full = join_episodes([season_episodes(n, ts) for n, ts in seasons[:2]], 3000, 20)
    assert full.partial_season is None


def test_missing_summary_tail_reaches_the_coverage_line() -> None:
    html = list_page_html({1: (2, 60), 2: (3, 60)}).replace(
        f"{words(60, 's2e3w')}", "")  # season 2's last episode has an empty summary
    fake = episode_fake(list_html=html)
    rec = fetch(fake)
    assert rec["episode_tables"]["partial_season"] == 2
    assert rec["episode_tables"]["partial_reason"] == "missing_summaries"
    assert rec["coverage"]["partial_season"] == 2
    gated, params = _request(rec)
    assert params["messages"][0]["content"][0]["text"].splitlines()[2] == (
        f"Summary covers seasons 1–2 of 9 (season 2 only in part{TAIL}")


@pytest.mark.parametrize(("path", "decides"), [
    (("season 1", "2005"), "season 1"),  # a year subheading inside a season: still season 1
    (("specials", "2010"), "specials"),  # a year subheading inside specials: still skipped
    (("episodes", "2010"), "2010"),  # no season or specials heading: the nearest one
])
def test_year_subheadings_do_not_decide_a_table(path: tuple[str, ...], decides: str) -> None:
    """QA: years are not season headings for ``table_heading`` (only the year rule reads
    them, from the path)."""
    from laminary_pipeline.ingest.episodes import table_heading

    assert table_heading(path) == decides


def test_season_and_specials_tables_with_year_subheadings_keep_their_meaning() -> None:
    html = (heading(2, "Season 1") + heading(3, "2005") + table(ep_rows(1, 2, 20))
            + heading(2, "Specials") + heading(3, "2010") + table(ep_rows(9, 1, 20)))
    seasons, skipped = list_page_seasons(parse_tables(html))
    assert [(n, len(ts)) for n, ts in seasons] == [(1, 1)]
    assert [s["heading"] for s in skipped] == ["specials"]


def test_part_subheadings_inside_a_season_keep_the_season() -> None:
    """QA S3: a table under h4 "Part 1" inside h3 "Season 5 (2012–13)" is season 5; a table
    under a "Specials" subheading inside a season is still skipped."""
    html = (
        heading(2, "Episodes") + heading(3, "Season 5 (2012–13)")
        + heading(4, "Part 1") + table(ep_rows(5, 2, 20))
        + heading(4, "Part 2") + table(ep_rows(5, 1, 20, first_overall=3))
        + heading(4, "Specials") + table(ep_rows(5, 1, 20, first_overall=90))
        + heading(3, "Season 6 (2014)") + table(ep_rows(6, 1, 20))
        + heading(2, "Home media") + table(ep_rows(7, 1, 20))
    )
    tables = parse_tables(html)
    assert [t.path for t in tables][0] == ("episodes", "season 5 (2012–13)", "part 1")
    assert [t.heading for t in tables] == ["season 5 (2012–13)", "season 5 (2012–13)",
                                           "specials", "season 6 (2014)", "home media"]
    seasons, skipped = list_page_seasons(tables)
    assert [(n, len(ts)) for n, ts in seasons] == [(5, 2), (6, 1)]
    assert [s["heading"] for s in skipped] == ["specials", "home media"]
    assert len(season_episodes(5, seasons[0][1]).episodes) == 3


def _two_list_pages(first: dict[int, tuple[int, int]], second: dict[int, tuple[int, int]],
                    *, first_summaries: bool = False) -> FakeWikimedia:
    fake = split_list_fake()
    w = fake.data["wikipedia"]
    (r1, r2) = (SPLIT_LISTS["List of Tidewater episodes: 1998–2009"][1],
                SPLIT_LISTS["List of Tidewater episodes: 2010–present"][1])
    html1 = list_page_html(first)
    if not first_summaries:
        html1 = html1.replace('<tr class="expand-child">', '<tr class="other">')
    w[f"page:{r1}"] = {"parse": {"text": html1}}
    w[f"page:{r2}"] = {"parse": {"text": list_page_html(second)}}
    return fake


def test_empty_seasons_on_an_earlier_page_do_not_block_a_later_page() -> None:
    """QA S4: rising order is checked against the seasons used. An earlier page whose seasons
    1-5 have no summaries doesn't block seasons 6-7 on the next page; its headings still count
    for the season total."""
    rec = fetch(_two_list_pages({n: (2, 40) for n in range(1, 6)}, {6: (2, 40), 7: (2, 40)}),
                seasons_total=None)
    assert rec["status"] == "ok"
    assert [p["source"]["season"] for p in rec["sources"]] == [6, 7]
    assert rec["coverage"]["total_seasons"] == 7
    assert rec["episode_tables"]["numbering_restart"] == []


def test_numbering_restart_across_pages_is_not_used_and_is_reported() -> None:
    """Doctor Who's shape: a classic page numbered Season 1-3 without summaries, then a
    revival page restarting at Series 1. Which run "of N" would count is unclear, so the
    revival's seasons are not used, and the restart is recorded for Hari."""
    fake = _two_list_pages({1: (2, 40), 2: (2, 40), 3: (2, 40)}, {1: (2, 40), 2: (2, 40)})
    rec = fetch(fake, seasons_total=None)
    assert rec["status"] == "skipped"
    assert "season numbering restarts across list pages" in rec["skip_detail"]
    assert rec["episode_tables"]["numbering_restart"] == [
        {"title": "List of Tidewater episodes: 2010–present", "season": 1,
         "earlier_pages_up_to": 3},
        {"title": "List of Tidewater episodes: 2010–present", "season": 2,
         "earlier_pages_up_to": 3}]


def test_season_too_long_series_are_fetched_again_by_1_5_0(tmp_path: Path) -> None:
    from laminary_pipeline.ingest.plots import needs_fetch

    paths = DataPaths.resolve(str(tmp_path))
    old = {**_skipped("Q1", "season_too_long"), "fetcher_version": "1.4.0"}
    write_json_atomic(paths.plot_file("Q1"), old)
    assert needs_fetch(paths, "Q1", False)
    write_json_atomic(paths.plot_file("Q1"), {**old, "fetcher_version": "1.5.0"})
    assert not needs_fetch(paths, "Q1", False)
