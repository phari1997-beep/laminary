"""Per-episode summaries from episode tables, the last fallback for big series (fetcher 1.5.0).

DECISIONS 2026-10-02: a series whose main article fails and whose season-article prose join
yields no usable text (under the 150-word minimum, or nothing at all) may use the per-episode
summaries in the episode tables of the same verified pages: its verified season pages
(P179/P361, ``seasons.py``) or, when no season page has any, its verified "List of <X> episodes"
page. English Wikipedia, CC BY-SA, no other source.

**Which cells.** MediaWiki renders ``{{Episode table}}`` / ``{{Episode list}}`` as a
``table.wikiepisodetable``: one ``tr.vevent`` row per episode (number cells, a ``td.summary``
title cell, then director, writer, air date, viewers, production code...) followed by a
``tr.expand-child`` row whose ``td.description`` holds the episode summary. Only two things are
taken: the ``td.description`` prose, through ``text.html_to_text`` (references, tables and
MediaWiki "Cite error" messages dropped), and a compact marker built from the season number,
the episode number and the ``td.summary`` title text. Every other cell (air dates, ratings,
writers, directors, production codes) and everything outside the tables is ignored.

**Which tables.** On a season page every episode table counts as that season's, except one
under a heading for specials, webisodes, minisodes or shorts (out of the season's order). On an
episode-list page a table's season is the number in the nearest enclosing heading, at any
level, that names one ("Season 3 (1991–92)", "Series 2"; a "Part 1" subheading inside "Season
5" is still season 5), unless a specials heading sits between them. Tables under other
headings are skipped, and seasons must rise down the page (a repeated or lower number stops
the page, e.g. a revival that restarts at "Series 1"). Every skipped table is recorded.

**Partial last season.** The last season used is marked ``partial_season`` when some of its
episodes didn't fit under the cap, or when rows after its last used episode have no summary
(episodes not yet aired or not yet summarized).

**Text.** One paragraph per episode, in season order then table order: ``S2E5 "Title": <summary>``
(``S2E5:`` without a title). The episode number is the table's in-season number when it has
one as a plain integer, else the row's position in the season. A season's paragraphs, joined by
blank lines, are one source; a list page therefore gives one source per season (same ``ref``
and revision, each with its own ``season``), so the coverage line and the gate keep working
from validated season numbers.

**Cap: episode boundary** (data-pipeline's choice). Episodes are added in order while the
total stays within ``SEASON_WORD_CAP`` (~3,000) words; the first episode that would cross it
stops the join, so the last season used may be included only in part (recorded as
``partial_season``). A season boundary would throw away a whole long season, often the first,
for one episode too many, and big procedurals have 22-episode seasons of ~150-word summaries
(~3,300 words), so stopping at a season boundary would leave many of them with nothing. Since
the join can always stop at an episode, no lead block over the cap is needed: episode-table
input never exceeds the ~3,000-word cap (the gate holds it to that), and the 6,000-word
ceiling does not apply. At most ``MAX_SEASON_SOURCES`` sources; the 150-word minimum applies
to the total as usual.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from laminary_pipeline.ingest.runs import SeriesRunRule
from laminary_pipeline.ingest.sections import normalize_heading
from laminary_pipeline.ingest.text import html_to_text, word_count

VIA_DETAIL_EPISODE_TABLE = "episode_table"
EPISODE_TABLE_CLASS = "wikiepisodetable"
MAX_TITLE_CHARS = 150
_LIST_SEASON_HEADING = re.compile(r"^(season|series)\s+(\d{1,3})\b")
# headings whose tables are out of a season's order (season pages): specials and extras
_SKIP_TABLE_HEADING = re.compile(r"\b(?:specials?|webisodes?|minisodes?|mobisodes?|shorts?)\b")
_PLAIN_INT = re.compile(r"^\d{1,3}$")
# year headings ("1998", "2024 (part 1)"), used only under a run rule with numbering "year"
# (fetcher 1.5.5, CID: DECISIONS 2026-10-02)
_YEAR_HEADING = re.compile(r"^(\d{4})\b")
MIN_YEAR, MAX_YEAR = 1900, 2100  # a year heading outside this range is not a year
UNIT_SEASON, UNIT_YEAR = "season", "year"
ROW_HEADER = " th"  # marks a <th> cell in the parser's class sets (never a real class name)
ROWSPAN = " rowspan="  # prefix of a pseudo-class carrying a cell's rowspan (same trick)


@dataclass(frozen=True)
class RawEpisode:
    """One ``tr.vevent`` row: the HTML of its number cells, its title cell and its summary."""

    number_cells: tuple[str, ...]  # cells before the title cell, as HTML
    title_html: str | None
    description_html: str | None
    # number cells of the rows a two-part title cell spans (rowspan="2": "Encounter at
    # Farpoint" is episodes 1 and 2 with one title and one summary; fetcher 1.5.7)
    extra_number_cells: tuple[tuple[str, ...], ...] = ()


@dataclass
class RawTable:
    heading: str  # the heading that decides the table (``table_heading``), normalized
    path: tuple[str, ...] = ()  # every enclosing heading, outermost first
    rows: list[RawEpisode] = field(default_factory=list)


class _TableParser(HTMLParser):
    """Episode tables of one page in document order, each with the heading above it. Cell HTML
    is re-serialized from the parsed tokens, so it can go through ``html_to_text``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[RawTable] = []
        self._headings: list[tuple[int, str]] = []  # enclosing headings: (level, text)
        self._heading_level = 0
        self._heading_parts: list[str] | None = None
        self._table_depth = 0  # open <table> tags inside the current episode table, itself 1
        self._row_kind: str | None = None  # "episode", "description" or None
        self._row_cells: list[tuple[set[str], str]] = []
        self._cell: list[str] | None = None
        self._cell_tag = ""
        self._cell_classes: set[str] = set()
        self._cell_nested_tables = 0
        self._pending: dict[str, Any] | None = None  # the last episode row, until its summary

    # --- helpers ---

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        return set((dict(attrs).get("class") or "").split())

    @staticmethod
    def _rowspan(classes: set[str]) -> int:
        for c in classes:
            if c.startswith(ROWSPAN) and c[len(ROWSPAN):].isdigit():
                return int(c[len(ROWSPAN):])
        return 1

    def _finish_row(self) -> None:
        has_title = any("summary" in classes for classes, _ in self._row_cells)
        starts_episode = has_title or any(ROW_HEADER in classes
                                          for classes, _ in self._row_cells)
        if (self._row_kind == "episode" and starts_episode and not has_title
                and self._pending is not None and self._pending["span_left"] > 0):
            # a new episode-number row (it has its own row header, "2") that the title cell
            # above still spans: a two-part episode (TNG "Encounter at Farpoint", episodes 1
            # and 2, one title and one summary). A spanned row without a row header (only a
            # second production code, Shrinking) stays a continuation, below.
            self._pending["span_left"] -= 1
            self._pending["extra"].append(tuple(cell for _, cell in self._row_cells))
        elif self._row_kind == "episode" and not starts_episode and self._pending is not None:
            # a continuation row of the episode above (its rowspan="2" cells span it, e.g. a
            # second production code): not a new episode
            pass
        elif self._row_kind == "episode":
            self._flush_pending()
            number_cells: list[str] = []
            title: str | None = None
            span = 1
            for classes, cell in self._row_cells:
                if "summary" in classes:
                    title, span = cell, self._rowspan(classes)
                    break
                number_cells.append(cell)
            self._pending = {"numbers": tuple(number_cells), "title": title, "desc": None,
                             "span_left": min(span, 4) - 1, "extra": []}
        elif self._row_kind == "description" and self._pending is not None:
            for classes, cell in self._row_cells:
                if "description" in classes and self._pending["desc"] is None:
                    self._pending["desc"] = cell
            self._flush_pending()
        self._row_kind = None
        self._row_cells = []

    def _flush_pending(self) -> None:
        if self._pending is not None and self.tables:
            p = self._pending
            self.tables[-1].rows.append(RawEpisode(p["numbers"], p["title"], p["desc"],
                                                   tuple(p.get("extra") or ())))
        self._pending = None

    # --- HTMLParser ---

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._cell is not None:
            if tag == "table":
                self._cell_nested_tables += 1
            self._cell.append(self.get_starttag_text() or f"<{tag}>")
            return
        if self._table_depth == 0:
            if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                self._heading_parts = []
                self._heading_level = int(tag[1])
            elif tag == "table" and EPISODE_TABLE_CLASS in self._classes(attrs):
                path = tuple(text for _, text in self._headings)
                self.tables.append(RawTable(table_heading(path), path))
                self._table_depth = 1
            return
        if tag == "table":
            self._table_depth += 1
        elif self._table_depth == 1 and tag == "tr":
            self._finish_row()
            classes = self._classes(attrs)
            self._row_kind = ("episode" if "vevent" in classes
                              else "description" if "expand-child" in classes else None)
        elif self._table_depth == 1 and tag in ("td", "th") and self._row_kind:
            self._cell, self._cell_tag = [], tag
            rowspan = (dict(attrs).get("rowspan") or "").strip()
            self._cell_classes = (self._classes(attrs) | ({ROW_HEADER} if tag == "th" else set())
                                  | ({ROWSPAN + rowspan} if rowspan.isdigit() else set()))
            self._cell_nested_tables = 0

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._cell is not None:
            self._cell.append(self.get_starttag_text() or f"<{tag} />")

    def handle_endtag(self, tag: str) -> None:
        if self._cell is not None:
            if tag == "table" and self._cell_nested_tables:
                self._cell_nested_tables -= 1
            elif tag == self._cell_tag and not self._cell_nested_tables:
                self._row_cells.append((self._cell_classes, "".join(self._cell)))
                self._cell = None
                return
            elif tag in ("tr", "table") and not self._cell_nested_tables:
                # an unclosed cell: close it, then handle the row or table end below
                self._row_cells.append((self._cell_classes, "".join(self._cell)))
                self._cell = None
            else:
                self._cell.append(f"</{tag}>")
                return
        if self._table_depth == 0:
            if tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self._heading_parts is not None:
                level = self._heading_level
                while self._headings and self._headings[-1][0] >= level:
                    self._headings.pop()
                self._headings.append((level, normalize_heading("".join(self._heading_parts))))
                self._heading_parts = None
            return
        if tag == "tr" and self._table_depth == 1:
            self._finish_row()
        elif tag == "table":
            self._table_depth -= 1
            if self._table_depth == 0:
                self._finish_row()
                self._flush_pending()

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(html.escape(data, quote=False))
        elif self._heading_parts is not None and self._table_depth == 0:
            self._heading_parts.append(data)

    def close(self) -> None:
        super().close()
        if self._table_depth:
            self._finish_row()
            self._flush_pending()


def table_heading(path: tuple[str, ...]) -> str:
    """The heading that decides a table, from its enclosing headings (outermost first): the
    nearest one that names specials or a season, else the nearest one ("" with none)."""
    for text in reversed(path):
        if _SKIP_TABLE_HEADING.search(text) or _LIST_SEASON_HEADING.match(text):
            return text
    return path[-1] if path else ""


def parse_tables(page_html: str) -> list[RawTable]:
    """Every ``wikiepisodetable`` on a rendered page, in document order, with its heading."""
    parser = _TableParser()
    parser.feed(page_html)
    parser.close()
    return parser.tables


@dataclass(frozen=True)
class Episode:
    season: int
    number: int
    title: str  # the title cell as text ("" when there is none)
    summary: str  # the description cell as text
    row: int = 0  # the episode row's position in its season (1-based), with or without summary
    # "season", or "year" when ``season`` is a broadcast year (a run rule, fetcher 1.5.5)
    unit: str = UNIT_SEASON
    # the last episode number of a two-part row ("S1E1–2", fetcher 1.5.7), else None
    number_end: int | None = None
    parts: int = 1  # episode rows this entry covers (2 for a two-part row)

    @property
    def marker(self) -> str:
        """"S2E5" for season 2 episode 5; "1998E5" for the fifth episode of year 1998;
        "S1E1–2" for a two-part episode with one title and summary."""
        number = (f"{self.number}–{self.number_end}"
                  if self.number_end is not None and self.number_end > self.number
                  else str(self.number))
        if self.unit == UNIT_YEAR:
            return f"{self.season}E{number}"
        return f"S{self.season}E{number}"

    @property
    def paragraph(self) -> str:
        marker = self.marker
        if self.title:
            marker = f"{marker} {self.title}"
        return f"{marker}: {self.summary}"

    @property
    def words(self) -> int:
        return word_count(self.paragraph)


@dataclass
class SeasonEpisodes:
    season: int
    episodes: list[Episode]
    rows: int  # episode rows in the season's tables, with or without a summary

    @property
    def without_summary(self) -> int:
        return self.rows - sum(e.parts for e in self.episodes)


def _title_text(cell: str | None) -> str:
    if not cell:
        return ""
    text = " ".join(html_to_text(cell).split())
    if len(text) > MAX_TITLE_CHARS:
        text = text[: MAX_TITLE_CHARS - 1].rsplit(" ", 1)[0] + "…"
    return text


def _episode_number(cells: tuple[str, ...], position: int) -> int:
    """The table's in-season number (the number cell just before the title, when the row has
    two or more number cells and it is a plain integer), else the row's position."""
    if len(cells) >= 2:
        value = html_to_text(cells[-1]).strip()
        if _PLAIN_INT.match(value) and int(value) >= 1:
            return int(value)
    return position


def season_episodes(
    season: int, tables: list[RawTable], unit: str = UNIT_SEASON
) -> SeasonEpisodes:
    """One season's (or, with ``unit="year"``, one year's) episodes with a summary, in table
    order."""
    out: list[Episode] = []
    position = 0
    for table in tables:
        for row in table.rows:
            position += 1
            number = _episode_number(row.number_cells, position)
            end = None
            for extra in row.extra_number_cells:  # a two-part row: one more episode each
                position += 1
                end = _episode_number(extra, position)
            summary = html_to_text(row.description_html) if row.description_html else ""
            summary = " ".join(summary.split())  # one paragraph per episode
            if not summary:
                continue
            out.append(Episode(season, number, _title_text(row.title_html), summary,
                               position, unit, end, 1 + len(row.extra_number_cells)))
    return SeasonEpisodes(season, out, position)


def season_page_tables(
    tables: list[RawTable],
) -> tuple[list[RawTable], list[dict[str, str]]]:
    """A season page's tables to use, and the skipped ones with a reason."""
    used, skipped = [], []
    for t in tables:
        if _SKIP_TABLE_HEADING.search(t.heading):
            skipped.append({"heading": t.heading, "reason": "specials or extras, out of the "
                            "season's order"})
        else:
            used.append(t)
    return used, skipped


def list_page_seasons(
    tables: list[RawTable], rule: SeriesRunRule | None = None,
) -> tuple[list[tuple[int, list[RawTable]]], list[dict[str, str]]]:
    """An episode-list page's tables grouped by the season named in the heading above them,
    in page order, and the skipped tables with a reason. Seasons must rise down the page; the
    first table under a repeated or lower season number stops the page. With a series run rule
    (fetcher 1.5.1, ``runs.py``), a heading in the other run's numbering ("Season 3" when the
    rule uses "Series N") is skipped before the rising check. With a rule numbering by year
    (fetcher 1.5.5, CID), each year heading ("1998") is one "season" numbered by its year, and
    the same rising order applies to years; season headings are then skipped."""
    if rule is not None and rule.numbering == UNIT_YEAR:
        return _list_page_years(tables, rule)
    seasons: list[tuple[int, list[RawTable]]] = []
    skipped: list[dict[str, str]] = []
    for i, t in enumerate(tables):
        m = _LIST_SEASON_HEADING.match(t.heading)
        if not m or _SKIP_TABLE_HEADING.search(t.heading):
            skipped.append({"heading": t.heading, "reason": "not under a season heading"})
            continue
        if rule is not None and not rule.heading_ok(m.group(1).casefold()):
            skipped.append({"heading": t.heading, "reason": rule.heading_reason()})
            continue
        n = int(m.group(2))
        if n < 1:
            skipped.append({"heading": t.heading, "reason": "season 0"})
            continue
        if seasons and n == seasons[-1][0]:
            seasons[-1][1].append(t)  # the same season continued in a second table
            continue
        if seasons and n <= seasons[-1][0]:
            skipped += [{"heading": r.heading, "reason": f"season {n} after season "
                         f"{seasons[-1][0]}: the page's numbering restarts; stopped here"}
                        for r in tables[i:]]
            break
        seasons.append((n, [t]))
    return seasons, skipped


def _list_page_years(
    tables: list[RawTable], rule: SeriesRunRule
) -> tuple[list[tuple[int, list[RawTable]]], list[dict[str, str]]]:
    """``list_page_seasons`` for a run rule numbering by year: tables grouped by the year
    heading above them, years rising down the page. The year comes from the table's enclosing
    headings (``RawTable.path``), nearest first, so only this rule reads year headings
    (``table_heading`` ignores them): a nearer specials heading skips the table, a nearer
    season heading is outside the run, and other headings ("Part 1") are passed over."""
    years: list[tuple[int, list[RawTable]]] = []
    skipped: list[dict[str, str]] = []
    for i, t in enumerate(tables):
        m, reason = None, "not under a year heading"
        for text in reversed(t.path):
            if _SKIP_TABLE_HEADING.search(text):
                reason = "specials or extras, out of the year's order"
                break
            if _LIST_SEASON_HEADING.match(text):
                reason = rule.heading_reason()
                break
            m = _YEAR_HEADING.match(text)
            if m:
                break
        if m is None:
            skipped.append({"heading": t.heading, "reason": reason})
            continue
        year = int(m.group(1))
        if not MIN_YEAR <= year <= MAX_YEAR:
            skipped.append({"heading": t.heading, "reason": f"{year} is not a plausible year"})
            continue
        if years and year == years[-1][0]:
            years[-1][1].append(t)
            continue
        if years and year < years[-1][0]:
            skipped += [{"heading": r.heading, "reason": f"year {year} after {years[-1][0]}: "
                         "the page's years go back; stopped here"} for r in tables[i:]]
            break
        years.append((year, [t]))
    return years, skipped


@dataclass
class EpisodeJoin:
    """The joined episode text: one entry per season used, plus what the cap left out."""

    seasons: list[SeasonEpisodes]  # each with only the episodes used
    # the last season used, when it is included only in part: some of its episodes didn't fit
    # under the cap ("word_cap"), or rows after its last used episode have no summary
    # ("missing_summaries", e.g. episodes not yet aired)
    partial_season: int | None = None
    partial_reason: str | None = None
    left_out_episodes: int = 0  # episodes with a summary left out over the cap
    left_out_seasons: list[int] = field(default_factory=list)  # seasons with none used
    stopped_by: str | None = None  # "word_cap" or "source_limit"

    @property
    def words(self) -> int:
        return sum(e.words for s in self.seasons for e in s.episodes)


def season_text(season: SeasonEpisodes) -> str:
    return "\n\n".join(e.paragraph for e in season.episodes)


def join_episodes(seasons: list[SeasonEpisodes], cap: int, max_sources: int) -> EpisodeJoin:
    """Seasons in the given (rising) order, episodes in table order, while the total stays
    within ``cap`` words and at most ``max_sources`` seasons; stops at the first episode that
    would cross the cap (an episode boundary). Seasons without any summary are passed over."""
    out = EpisodeJoin([])
    total = 0
    stopped = False
    for s in seasons:
        if not s.episodes:
            continue
        if not stopped and len(out.seasons) >= max_sources:
            stopped, out.stopped_by = True, "source_limit"
        if stopped:
            out.left_out_episodes += len(s.episodes)
            out.left_out_seasons.append(s.season)
            continue
        kept: list[Episode] = []
        for ep in s.episodes:
            if total + ep.words > cap:
                stopped, out.stopped_by = True, "word_cap"
                break
            kept.append(ep)
            total += ep.words
        out.left_out_episodes += len(s.episodes) - len(kept)
        if kept:
            out.seasons.append(SeasonEpisodes(s.season, kept, s.rows))
            if len(kept) < len(s.episodes):
                out.partial_season, out.partial_reason = s.season, "word_cap"
        else:
            out.left_out_seasons.append(s.season)
    if out.seasons and out.partial_season is None:
        last = out.seasons[-1]
        if last.rows > last.episodes[-1].row:  # rows without a summary after the last one used
            out.partial_season, out.partial_reason = last.season, "missing_summaries"
    return out
