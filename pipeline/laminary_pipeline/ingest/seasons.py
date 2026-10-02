"""Per-season English Wikipedia articles for series whose main article is too thin.

DECISIONS 2026-10-01: when a series' main enwiki article fails the 150-word rule or has no plot
section, its per-season articles may supply the summary instead. Many recent series keep their
story there; the main article holds only a premise.

1. **Find** candidate pages (all on en.wikipedia.org, main namespace only):
   - links in the main article (pinned revision) whose title starts with the series name and
     ends in "season N" / "series N" (with or without parentheses), or is
     "List of <series> episodes", or (fetcher 1.5.0) such a page split by a year range, season
     range or part (": 1998–2009", " (1998–2009)", " (seasons 1–5)", " (part 1)"; see
     ``episode_list_order``), ordered by its year, season or part;
   - guessed titles "<X> season N", "<X> (season N)", "<X> series N", "<X> (series N)" for
     N = 1..MAX_SEASONS, and "List of <X> episodes", where <X> is the main page title without
     its "(TV series)"-style disambiguator.
   Existing pages are resolved in batches (redirects followed); missing, non-main-namespace and
   disambiguation pages, and redirects back to the main article, are dropped.
2. **Verify** each page: its Wikidata item (``wikibase_item``) must state "part of the series"
   (P179) or "part of" (P361) with the series' QID as the value. Pages without that positive
   evidence are skipped and listed. A P179 series ordinal (P1545) that disagrees with the season
   number in the title also skips the page. The matched statement (property, series QID,
   ordinal) is kept per used page and written to the plot file (``season_articles.evidence``,
   DECISIONS 2026-10-02).
3. **Text:** for each verified season page, in season order, the top-level plot, summary or
   synopsis sections and a prose season overview, through the same ``html_to_text`` (tables,
   so episode tables, are dropped) and the same non-plot filter as main articles
   (``sections.py``: production, broadcast and reception subsections dropped). A "List of <X>
   episodes" page is used only when no season page yields text. When this prose join yields
   no usable text either, the fetcher falls back to the episode tables of the same verified
   pages (fetcher 1.5.0, ``episodes.py``), using ``SeasonResult.season_pages`` and
   ``list_pages``.
4. **Cap:** seasons are added in order while the total stays within SEASON_WORD_CAP words and
   MAX_SEASON_SOURCES pages. The first season that would cross the cap stops the join, and it
   and later seasons are left out; a season is never cut mid-way. Lead block (DECISIONS
   2026-10-01, stub threshold raised to 500 words the same day): a *stub* season is one whose
   own text is under STUB_SEASON_WORDS (500) words, judged per season. Stub seasons before the
   first full season (500+ words) are kept and joined, in order, with that season; seasons
   that are missing, unverified or have no text don't count. That lead block may run past the
   cap, up to LEAD_BLOCK_CEILING words. Over the ceiling (fallback, DECISIONS 2026-10-01): if
   stubs lead it, the full season and every later page are left out and the stubs are used
   alone, joined under the cap as stubs only (below); a lone full season over the ceiling, with
   no stubs before it, skips the title (``season_too_long``, no list-page fallback). If the
   lead block is over the cap nothing more is added; otherwise later seasons join under the
   cap as above. With no full season at all (or when MAX_SEASON_SOURCES is reached before
   one), the stubs join under the cap as usual. The fetcher's separate 150-word minimum then
   applies to the joined total (so 100 + 5,901 words ends as too_short on the 100-word stub).

Each page becomes its own source (``kind: wikipedia_plot``, CC BY-SA, ref, revision,
``content_sha256`` of that page's text, ``season``). The annotation request sends one summary
block per source, in this order, each opened by a marker naming its article, which is the
season marker the model and gold labelers see.

**Coverage** (``season_coverage``, DECISIONS 2026-10-02): which seasons the joined text covers
and how many seasons the series has, so the annotation request can say "Summary covers seasons
1-4 of 7" when seasons are left out or missing. The total is Wikidata's "number of seasons"
(P2437, from the candidate's detail query) when it is a whole number at least as large as every
verified season page; otherwise the highest season number among the verified season pages
found (pages left out over the cap and pages with no plot text count, unverified pages don't).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from laminary_pipeline.ingest.sections import normalize_heading
from laminary_pipeline.ingest.text import word_count
from laminary_pipeline.ingest.wikidata import PREFIXES, SPARQL_URL

if TYPE_CHECKING:
    from laminary_pipeline.ingest.wikipedia import PlotFetcher

SEASON_WORD_CAP = 3000  # DECISIONS 2026-10-01
# A season whose own text is under this many words is a stub for the lead block (DECISIONS
# 2026-10-01, raised from 150). Separate from the fetcher's 150-word minimum, which still
# decides whether the joined text is usable at all.
STUB_SEASON_WORDS = 500
# Lead block (DECISIONS 2026-10-01): the first full season (STUB_SEASON_WORDS+ words) with the
# seasons before it that are missing/unverified/no text, or are stubs (the stubs are joined
# with it), is used whole even over the cap, up to this hard ceiling. Above it the stubs are
# used alone under the cap, or, with no stubs, the title is skipped (season_too_long).
LEAD_BLOCK_CEILING = 6000
SEASON_ONE_CEILING = LEAD_BLOCK_CEILING  # the name before fetcher 1.4.0
MAX_SEASON_NUMBER = 100  # the schema's source.season maximum
MAX_SEASONS = 20  # season numbers guessed by title
MAX_SEASON_SOURCES = 20  # also the schema's provenance.sources maxItems
QUERY_BATCH = 50  # MediaWiki's limit on titles per query
MIN_OVERVIEW_WORDS = 20  # an overview with less prose than this is a table remnant
SEASON_CHECK_TTL = 7 * 24 * 3600

SEASON_PLOT_HEADINGS = frozenset(
    {"plot", "plot summary", "synopsis", "summary", "story", "storyline", "season synopsis",
     "season summary", "plot synopsis"}
)
SEASON_OVERVIEW_HEADINGS = frozenset({"overview", "season overview", "series overview"})

QID_RE = re.compile(r"^Q[1-9][0-9]*$")  # checked before any id goes into SPARQL or int()
_DISAMBIGUATOR = re.compile(r"\s+\([^()]*\)$")
_SEASON_NUMBER = re.compile(r"\b(?:season|series)\s+(\d{1,2})\)?$", re.IGNORECASE)


def series_base(page_title: str) -> str:
    """'Succession (TV series)' -> 'Succession'."""
    return _DISAMBIGUATOR.sub("", page_title).strip()


def season_number(title: str, base: str) -> int | None:
    """The season number of a season article title of this series, else None."""
    if not title.casefold().startswith(base.casefold()):
        return None
    m = _SEASON_NUMBER.search(title)
    return int(m.group(1)) if m and int(m.group(1)) >= 1 else None


# Episode-list pages split by a year range or a numbered part (fetcher 1.5.0): the suffix after
# "List of <X> episodes". Each kind maps to its order rank and the number that orders it.
_DASH = r"\s*[-–—]\s*"
_LIST_SPLITS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (1, re.compile(rf"^:\s*(\d{{4}}){_DASH}(?:\d{{4}}|present)$")),  # ": 1998–2009"
    (1, re.compile(rf"^\s*\((\d{{4}}){_DASH}(?:\d{{4}}|present)\)$")),  # " (1998–2009)"
    (2, re.compile(rf"^\s*\((?:seasons|series)\s+(\d{{1,3}}){_DASH}\d{{1,3}}\)$")),
    (3, re.compile(r"^\s*\(part\s+(\d{1,2})\)$")),  # " (part 1)"
)
MAX_LIST_PARTS = 4  # "List of <X> episodes (part N)" titles guessed


def episode_list_order(title: str, base: str, page_title: str) -> tuple[int, int] | None:
    """For an episode-list page of this series, its sort key: (0, 0) for the plain "List of
    <X> episodes", else (kind, start) for a page split by a year range (": 1998–2009",
    " (1998–2009)", "–present" too), a season range (" (seasons 1–5)") or a part (" (part 1)"),
    so split pages sort by their year, season or part. None for any other title."""
    t = title.casefold()
    for name in (base, page_title):
        prefix = f"list of {name} episodes".casefold()
        if not t.startswith(prefix):
            continue
        rest = t[len(prefix):]
        if rest == "":
            return (0, 0)
        for kind, pattern in _LIST_SPLITS:
            m = pattern.match(rest)
            if m:
                return (kind, int(m.group(1)))
    return None


def is_episode_list(title: str, base: str, page_title: str) -> bool:
    return episode_list_order(title, base, page_title) is not None


def guessed_titles(base: str, page_title: str, max_seasons: int = MAX_SEASONS) -> list[str]:
    titles = []
    for n in range(1, max_seasons + 1):
        titles += [f"{base} season {n}", f"{base} (season {n})", f"{base} series {n}",
                   f"{base} (series {n})"]
    titles.append(f"List of {base} episodes")
    if page_title != base:
        titles.append(f"List of {page_title} episodes")
    # year- and season-range splits can't be guessed; they are found through the main
    # article's links
    titles += [f"List of {base} episodes (part {n})" for n in range(1, MAX_LIST_PARTS + 1)]
    return titles


def season_check_sparql(series_qid: str, items: list[str]) -> str:
    """One row per matching statement: the item, the property (P179 or P361) and, for P179,
    the series ordinal qualifier (P1545) if stated."""
    bad = [q for q in [series_qid, *items] if not QID_RE.match(q)]
    if bad:
        raise ValueError(f"not Wikidata item ids: {bad}")
    values = " ".join(f"wd:{q}" for q in sorted(set(items), key=lambda q: int(q[1:])))
    return f"""{PREFIXES}PREFIX p: <http://www.wikidata.org/prop/>
PREFIX ps: <http://www.wikidata.org/prop/statement/>
PREFIX pq: <http://www.wikidata.org/prop/qualifier/>
# laminary season check: {series_qid}
SELECT ?item ?prop ?ordinal WHERE {{
  VALUES ?item {{ {values} }}
  {{ ?item p:P179 ?st_ . ?st_ ps:P179 wd:{series_qid} .
     OPTIONAL {{ ?st_ pq:P1545 ?ordinal . }}
     BIND("P179" AS ?prop) }}
  UNION
  {{ ?item wdt:P361 wd:{series_qid} . BIND("P361" AS ?prop) }}
}}
"""


@dataclass(frozen=True)
class Evidence:
    """One Wikidata statement placing a page's item in the series."""

    item: str
    property: str  # "P179" (part of the series) or "P361" (part of)
    series: str
    ordinal: int | None  # P179's series ordinal (P1545), when stated as a whole number

    def record(self) -> dict[str, Any]:
        return {"item": self.item, "property": self.property, "series": self.series,
                "ordinal": self.ordinal}


def choose_evidence(statements: list[Evidence], season: int | None) -> Evidence | None:
    """The statement to record for a page: a P179 whose ordinal is the season, else a P179
    without an ordinal, else any P179, else P361. None for no statements."""
    if not statements:
        return None

    def rank(e: Evidence) -> tuple[int, int, str]:
        if e.property == "P179" and season is not None and e.ordinal == season:
            r = 0
        elif e.property == "P179" and e.ordinal is None:
            r = 1
        elif e.property == "P179":
            r = 2
        else:
            r = 3
        return (r, e.ordinal or 0, e.item)

    return sorted(statements, key=rank)[0]


def lead_block_ceiling(report: Mapping[str, Any]) -> int | None:
    """The lead-block ceiling a plot file's ``season_articles`` report was made with; files
    from fetchers before 1.4.0 call it ``season_one_ceiling``."""
    value = report.get("lead_block_ceiling", report.get("season_one_ceiling"))
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def whole_seasons(value: Any) -> int | None:
    """A season count as a plain int from 1 to MAX_SEASON_NUMBER, else None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= MAX_SEASON_NUMBER else None


COVERAGE_BASES = ("wikidata_P2437", "verified_season_pages")


def season_coverage(
    used: list[int | None], verified: list[int], wikidata_total: Any
) -> dict[str, Any] | None:
    """Which seasons a season-article text covers and the series' season total (see the module
    docstring). None when a used page has no season number (an episode-list page)."""
    if not used or any(whole_seasons(s) is None for s in used):
        return None
    seasons = sorted(int(s) for s in used if s is not None)
    pages = [n for n in verified if whole_seasons(n) is not None]
    highest = max([*pages, *seasons])
    out: dict[str, Any] = {"seasons": seasons}
    wd_total = whole_seasons(wikidata_total)
    if wd_total is not None and wd_total >= highest:
        out.update(total_seasons=wd_total, total_seasons_basis="wikidata_P2437")
    else:
        out.update(total_seasons=highest, total_seasons_basis="verified_season_pages")
        if wikidata_total is not None:
            out["wikidata_number_of_seasons_ignored"] = wikidata_total
    total = out["total_seasons"]
    out["partial"] = seasons != list(range(1, total + 1))
    return out


@dataclass(frozen=True)
class Page:
    title: str
    pageid: int
    revid: int
    timestamp: str
    item: str | None
    season: int | None  # None for a list-of-episodes page
    evidence: Evidence | None = None  # the statement that verified it


@dataclass
class SeasonText:
    page: Page
    text: str
    words: int
    headings: list[str]
    checks: list[dict[str, Any]] = field(default_factory=list)  # per section, sections.py


@dataclass
class SeasonResult:
    texts: list[SeasonText]
    skipped: list[dict[str, str]]  # {"title", "reason"}
    left_out_over_cap: list[str]
    used_list_page: bool
    too_long: str | None = None  # lone full season over the ceiling: "<title> (<n> words)"
    verified_seasons: list[int] = field(default_factory=list)  # every verified season page
    # the verified pages found (fetcher 1.5.0: the episode-table fallback reuses them): season
    # pages with a unique season number, in season order, and episode-list pages
    season_pages: list[Page] = field(default_factory=list)
    list_pages: list[Page] = field(default_factory=list)

    @property
    def words(self) -> int:
        return sum(t.words for t in self.texts)


class SeasonFinder:
    """Season-article lookup for one PlotFetcher (same HTTP client, throttle and cache)."""

    def __init__(
        self, fetcher: PlotFetcher, *, cap: int = SEASON_WORD_CAP,
        ceiling: int = LEAD_BLOCK_CEILING, stub_words: int = STUB_SEASON_WORDS,
    ) -> None:
        self.fetcher = fetcher
        self.cap = cap
        self.ceiling = ceiling
        self.stub_words = stub_words  # not fetcher.min_words: the two thresholds are separate

    # ---------- discovery ----------

    def linked_titles(self, revid: int, base: str, page_title: str) -> list[str]:
        data = self.fetcher._api(
            {"action": "parse", "oldid": str(revid), "prop": "links"}, cache_ttl=None
        )
        out = []
        for link in data.get("parse", {}).get("links", []):
            title = link.get("title", "")
            if link.get("ns") != 0 or not link.get("exists", True):
                continue
            if season_number(title, base) or is_episode_list(title, base, page_title):
                out.append(title)
        return out

    def resolve(self, titles: list[str]) -> list[dict[str, Any]]:
        """Existing main-namespace pages for ``titles`` (redirects followed), each once."""
        pages: dict[int, dict[str, Any]] = {}
        unique = list(dict.fromkeys(titles))
        for i in range(0, len(unique), QUERY_BATCH):
            data = self.fetcher._api(
                {
                    "action": "query", "titles": "|".join(unique[i : i + QUERY_BATCH]),
                    "redirects": "1", "prop": "revisions|pageprops", "rvprop": "ids|timestamp",
                    "ppprop": "wikibase_item|disambiguation",
                },
                cache_ttl=SEASON_CHECK_TTL,
            )
            for page in data.get("query", {}).get("pages", []):
                if page.get("missing") or page.get("invalid") or page.get("ns") != 0:
                    continue
                if not page.get("revisions") or "disambiguation" in (page.get("pageprops") or {}):
                    continue
                pages[int(page["pageid"])] = page
        return [pages[k] for k in sorted(pages)]

    def verified_items(self, series_qid: str, items: list[str]) -> dict[str, list[Evidence]]:
        """Item -> the statements placing it in the series (P179, with its ordinal if stated
        as a whole number, or P361); items without such a statement are absent."""
        if not items:
            return {}
        data = self.fetcher.client.post_form_json(
            SPARQL_URL,
            {"query": season_check_sparql(series_qid, items), "format": "json"},
            headers={"Accept": "application/sparql-results+json"},
            cache_ttl=SEASON_CHECK_TTL,
        )
        wanted = set(items)
        out: dict[str, list[Evidence]] = {}
        for b in data["results"]["bindings"]:
            qid = b["item"]["value"].rsplit("/", 1)[-1]
            prop = (b.get("prop") or {}).get("value")
            if qid not in wanted or prop not in ("P179", "P361"):
                continue  # only statements the query asked for count as evidence
            raw = (b.get("ordinal") or {}).get("value")
            ordinal = int(raw) if prop == "P179" and raw and raw.isdigit() else None
            ev = Evidence(qid, prop, series_qid, ordinal)
            if ev not in out.setdefault(qid, []):
                out[qid].append(ev)
        return out

    # ---------- main entry ----------

    def find(self, series_qid: str, page_title: str, page_id: int, revid: int) -> SeasonResult:
        base = series_base(page_title)
        if not QID_RE.match(series_qid):
            return SeasonResult([], [{"title": page_title, "reason": f"series id {series_qid!r} "
                                      "is not a Wikidata item id"}], [], False)
        titles = self.linked_titles(revid, base, page_title) + guessed_titles(base, page_title)
        skipped: list[dict[str, str]] = []
        raw_pages = [p for p in self.resolve(titles) if int(p["pageid"]) != page_id]
        pages: list[Page] = []
        for p in raw_pages:
            title = p["title"]
            number = season_number(title, base)
            if number is None and not is_episode_list(title, base, page_title):
                skipped.append({"title": title, "reason": "not a season or episode-list page"})
                continue
            item = (p.get("pageprops") or {}).get("wikibase_item")
            if item is not None and not (isinstance(item, str) and QID_RE.match(item)):
                skipped.append({"title": title, "reason": f"malformed Wikidata item {item!r}"})
                continue
            rev = p["revisions"][0]
            pages.append(Page(title, int(p["pageid"]), int(rev["revid"]), rev["timestamp"],
                              item, number))
        evidence = self.verified_items(series_qid, [p.item for p in pages if p.item])
        verified: list[Page] = []
        for p in pages:
            statements = evidence.get(p.item or "", [])
            ordinals = sorted({e.ordinal for e in statements if e.ordinal is not None})
            if p.item is None or not statements:
                skipped.append({"title": p.title, "reason": f"unverified: Wikidata item {p.item} "
                                f"is not stated as part of {series_qid} (P179/P361)"})
            elif p.season is not None and ordinals and p.season not in ordinals:
                shown = ", ".join(map(str, ordinals))
                skipped.append({"title": p.title, "reason": f"series ordinal {shown} "
                                f"disagrees with season {p.season} in the title"})
            else:
                verified.append(replace(p, evidence=choose_evidence(statements, p.season)))
        seasons = [p for p in verified if p.season is not None]
        counts = Counter(p.season for p in seasons)
        for p in seasons:
            if counts[p.season] > 1:
                skipped.append({"title": p.title, "reason": f"two pages claim season {p.season}"})
        seasons = sorted(
            (p for p in seasons if counts[p.season] == 1), key=lambda p: p.season or 0
        )
        # episode-list pages, the plain one first, then split pages by year, season or part
        lists = sorted(
            (p for p in verified if p.season is None),
            key=lambda p: (episode_list_order(p.title, base, page_title) or (9, 0), p.title),
        )
        verified_seasons = sorted({p.season for p in verified if p.season is not None})

        result = self._join(seasons, skipped)
        if not result.texts and lists and result.too_long is None:
            # keep what the season join left out (QA nit 3). With the lead block a season join
            # only ends empty when no season has text (left_out is then empty) or a lone full
            # season is over the ceiling (no fallback), so this is defensive. Stubs alone
            # (the over-the-ceiling fallback) always keep at least one stub.
            left_out = result.left_out_over_cap
            result = self._join(lists[:1], skipped)
            result.left_out_over_cap = left_out + result.left_out_over_cap
            result.used_list_page = True
        result.verified_seasons = verified_seasons
        result.season_pages = seasons
        result.list_pages = lists
        return result

    def _season_text(self, page: Page) -> SeasonText | None:
        sections = self.fetcher.sections(page.revid)
        texts, headings, checks = [], [], []
        for s in sections:
            index = str(s.get("index", ""))
            if not index.isdigit() or str(s.get("toclevel")) != "1":
                continue
            if s.get("fromtitle") and s["fromtitle"].replace("_", " ") != page.title:
                continue
            heading = normalize_heading(s.get("line", ""))
            is_overview = heading in SEASON_OVERVIEW_HEADINGS
            if heading not in SEASON_PLOT_HEADINGS and not is_overview:
                continue
            filtered = self.fetcher.section_filtered(page.revid, index, heading)
            checks.append(filtered.record(index))
            text = filtered.text
            if not text or (is_overview and filtered.words < MIN_OVERVIEW_WORDS):
                continue  # an overview that was only a table, or non-plot parts only
            texts.append(text)
            headings.append(heading)
        if not texts:
            return None
        joined = "\n\n".join(texts)
        return SeasonText(page, joined, word_count(joined), headings, checks)

    def _join(self, pages: list[Page], skipped: list[dict[str, str]]) -> SeasonResult:
        """Seasons in order; see the module docstring, step 4. Pages are fetched one at a time
        and the join stops at the first one left out, so later pages are never fetched."""
        out = SeasonResult([], skipped, [], False)
        total = 0
        in_lead = True  # until the first full season: stubs before it join the lead block
        for i, page in enumerate(pages):
            st = self._season_text(page)
            if st is None:
                skipped.append({"title": page.title, "reason": "no plot, summary or synopsis "
                                "section with prose"})
                continue
            if len(out.texts) >= MAX_SEASON_SOURCES:
                out.left_out_over_cap = [p.title for p in pages[i:]]
                break
            if in_lead and page.season is not None:
                out.texts.append(st)
                total += st.words
                if st.words < self.stub_words:
                    continue  # a stub season: kept for the first full season after it
                in_lead = False
                if total > self.ceiling and len(out.texts) > 1:
                    # stubs + the full season over the ceiling (DECISIONS 2026-10-01, fallback):
                    # drop the full season and later pages; the stubs-only trim below applies
                    full = out.texts.pop()
                    total -= full.words
                    in_lead = True
                    out.left_out_over_cap = [full.page.title] + [p.title for p in pages[i + 1 :]]
                    break
                if total > self.ceiling:  # a lone full season over the ceiling: skip the title
                    out.too_long = f"{st.page.title} ({total} words)"
                    out.left_out_over_cap = [p.title for p in pages[i:]]
                    out.texts = []
                    break
                if total > self.cap:  # the lead block over the cap: used alone, nothing added
                    out.left_out_over_cap = [p.title for p in pages[i + 1 :]]
                    break
                continue  # the lead block within the cap: later seasons join under the cap
            in_lead = False
            if total + st.words > self.cap:
                out.left_out_over_cap = [p.title for p in pages[i:]]
                break
            out.texts.append(st)
            total += st.words
        if in_lead and total > self.cap:
            # stubs only (no full season, or the source limit came first): the cap applies as
            # in normal joining
            kept, words = [], 0
            for t in out.texts:
                if words + t.words > self.cap:
                    break
                kept.append(t)
                words += t.words
            dropped = [t.page.title for t in out.texts[len(kept) :]]
            out.texts = kept
            out.left_out_over_cap = dropped + out.left_out_over_cap
        return out
