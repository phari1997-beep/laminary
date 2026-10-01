"""Per-season English Wikipedia articles for series whose main article is too thin.

DECISIONS 2026-10-01: when a series' main enwiki article fails the 150-word rule or has no plot
section, its per-season articles may supply the summary instead. Many recent series keep their
story there; the main article holds only a premise.

1. **Find** candidate pages (all on en.wikipedia.org, main namespace only):
   - links in the main article (pinned revision) whose title starts with the series name and
     ends in "season N" / "series N" (with or without parentheses), or is
     "List of <series> episodes";
   - guessed titles "<X> season N", "<X> (season N)", "<X> series N", "<X> (series N)" for
     N = 1..MAX_SEASONS, and "List of <X> episodes", where <X> is the main page title without
     its "(TV series)"-style disambiguator.
   Existing pages are resolved in batches (redirects followed); missing, non-main-namespace and
   disambiguation pages, and redirects back to the main article, are dropped.
2. **Verify** each page: its Wikidata item (``wikibase_item``) must state "part of the series"
   (P179) or "part of" (P361) with the series' QID as the value. Pages without that positive
   evidence are skipped and listed. A P179 series ordinal (P1545) that disagrees with the season
   number in the title also skips the page.
3. **Text:** for each verified season page, in season order, the top-level plot, summary or
   synopsis sections and a prose season overview, through the same ``html_to_text`` (tables,
   so episode tables, are dropped). A "List of <X> episodes" page is used only when no season
   page yields text.
4. **Cap:** seasons are added in order while the total stays within SEASON_WORD_CAP words and
   MAX_SEASON_SOURCES pages. The first season that would cross the cap stops the join, and it
   and later seasons are left out; a season is never cut mid-way. Exception: if season 1 alone
   is over the cap, season 1 is used in full and alone, up to SEASON_ONE_CEILING words; a
   longer season 1 skips the title (``season_too_long``). The 150-word rule then applies to
   the joined total.

Each page becomes its own source (``kind: wikipedia_plot``, CC BY-SA, ref, revision,
``content_sha256`` of that page's text, ``season``). The annotation request sends one summary
block per source, in this order, each opened by a marker naming its article, which is the
season marker the model and gold labelers see.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from laminary_pipeline.ingest.text import word_count
from laminary_pipeline.ingest.wikidata import PREFIXES, SPARQL_URL

if TYPE_CHECKING:
    from laminary_pipeline.ingest.wikipedia import PlotFetcher

SEASON_WORD_CAP = 3000  # DECISIONS 2026-10-01
# First-season exception (DECISIONS 2026-10-01): season 1 is used whole, alone, even over the
# cap, up to this hard ceiling; above it the title is skipped (season_too_long).
SEASON_ONE_CEILING = 6000
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


def is_episode_list(title: str, base: str, page_title: str) -> bool:
    t = title.casefold()
    return t in (f"list of {base} episodes".casefold(), f"list of {page_title} episodes".casefold())


def guessed_titles(base: str, page_title: str, max_seasons: int = MAX_SEASONS) -> list[str]:
    titles = []
    for n in range(1, max_seasons + 1):
        titles += [f"{base} season {n}", f"{base} (season {n})", f"{base} series {n}",
                   f"{base} (series {n})"]
    titles.append(f"List of {base} episodes")
    if page_title != base:
        titles.append(f"List of {page_title} episodes")
    return titles


def season_check_sparql(series_qid: str, items: list[str]) -> str:
    bad = [q for q in [series_qid, *items] if not QID_RE.match(q)]
    if bad:
        raise ValueError(f"not Wikidata item ids: {bad}")
    values = " ".join(f"wd:{q}" for q in sorted(set(items), key=lambda q: int(q[1:])))
    return f"""{PREFIXES}PREFIX p: <http://www.wikidata.org/prop/>
PREFIX ps: <http://www.wikidata.org/prop/statement/>
PREFIX pq: <http://www.wikidata.org/prop/qualifier/>
# laminary season check: {series_qid}
SELECT ?item (SAMPLE(?ordinal_) AS ?ordinal) WHERE {{
  VALUES ?item {{ {values} }}
  {{ ?item p:P179 ?st_ . ?st_ ps:P179 wd:{series_qid} .
     OPTIONAL {{ ?st_ pq:P1545 ?ordinal_ . }} }}
  UNION
  {{ ?item wdt:P361 wd:{series_qid} . }}
}}
GROUP BY ?item
"""


@dataclass(frozen=True)
class Page:
    title: str
    pageid: int
    revid: int
    timestamp: str
    item: str | None
    season: int | None  # None for a list-of-episodes page


@dataclass
class SeasonText:
    page: Page
    text: str
    words: int
    headings: list[str]


@dataclass
class SeasonResult:
    texts: list[SeasonText]
    skipped: list[dict[str, str]]  # {"title", "reason"}
    left_out_over_cap: list[str]
    used_list_page: bool
    too_long: str | None = None  # season 1 over SEASON_ONE_CEILING: "<title> (<n> words)"

    @property
    def words(self) -> int:
        return sum(t.words for t in self.texts)


class SeasonFinder:
    """Season-article lookup for one PlotFetcher (same HTTP client, throttle and cache)."""

    def __init__(
        self, fetcher: PlotFetcher, *, cap: int = SEASON_WORD_CAP,
        ceiling: int = SEASON_ONE_CEILING,
    ) -> None:
        self.fetcher = fetcher
        self.cap = cap
        self.ceiling = ceiling

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

    def verified_items(self, series_qid: str, items: list[str]) -> dict[str, int | None]:
        """Item -> P179 series ordinal (None if not stated) for items Wikidata places in the
        series; items without that evidence are absent."""
        if not items:
            return {}
        data = self.fetcher.client.post_form_json(
            SPARQL_URL,
            {"query": season_check_sparql(series_qid, items), "format": "json"},
            headers={"Accept": "application/sparql-results+json"},
            cache_ttl=SEASON_CHECK_TTL,
        )
        out: dict[str, int | None] = {}
        for b in data["results"]["bindings"]:
            qid = b["item"]["value"].rsplit("/", 1)[-1]
            raw = (b.get("ordinal") or {}).get("value")
            out[qid] = int(raw) if raw and raw.isdigit() else None
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
            if p.item is None or p.item not in evidence:
                skipped.append({"title": p.title, "reason": f"unverified: Wikidata item {p.item} "
                                f"is not stated as part of {series_qid} (P179/P361)"})
            elif p.season is not None and evidence[p.item] not in (None, p.season):
                skipped.append({"title": p.title, "reason": f"series ordinal {evidence[p.item]} "
                                f"disagrees with season {p.season} in the title"})
            else:
                verified.append(p)
        seasons = [p for p in verified if p.season is not None]
        counts = Counter(p.season for p in seasons)
        for p in seasons:
            if counts[p.season] > 1:
                skipped.append({"title": p.title, "reason": f"two pages claim season {p.season}"})
        seasons = sorted(
            (p for p in seasons if counts[p.season] == 1), key=lambda p: p.season or 0
        )
        lists = [p for p in verified if p.season is None]

        result = self._join(seasons, skipped)
        if not result.texts and lists and result.too_long is None:
            # keep what the season join left out (QA nit 3); with the season-1 exception a
            # season join only ends empty when every season lacks text or season 1 is too long
            left_out = result.left_out_over_cap
            result = self._join(lists[:1], skipped)
            result.left_out_over_cap = left_out + result.left_out_over_cap
            result.used_list_page = True
        return result

    def _season_text(self, page: Page) -> SeasonText | None:
        from laminary_pipeline.ingest.wikipedia import normalize_heading  # circular at import

        sections = self.fetcher.sections(page.revid)
        texts, headings = [], []
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
            text = self.fetcher.section_text(page.revid, index)
            if not text or (is_overview and word_count(text) < MIN_OVERVIEW_WORDS):
                continue  # an overview that was only a table
            texts.append(text)
            headings.append(heading)
        if not texts:
            return None
        joined = "\n\n".join(texts)
        return SeasonText(page, joined, word_count(joined), headings)

    def _join(self, pages: list[Page], skipped: list[dict[str, str]]) -> SeasonResult:
        out = SeasonResult([], skipped, [], False)
        total = 0
        for i, page in enumerate(pages):
            st = self._season_text(page)
            if st is None:
                skipped.append({"title": page.title, "reason": "no plot, summary or synopsis "
                                "section with prose"})
                continue
            if not out.texts and page.season == 1 and st.words > self.cap:
                # first-season exception: season 1 whole and alone, or nothing
                if st.words > self.ceiling:
                    out.too_long = f"{page.title} ({st.words} words)"
                    out.left_out_over_cap = [p.title for p in pages[i:]]
                else:
                    out.texts.append(st)
                    out.left_out_over_cap = [p.title for p in pages[i + 1 :]]
                break
            if total + st.words > self.cap or len(out.texts) >= MAX_SEASON_SOURCES:
                out.left_out_over_cap = [p.title for p in pages[i:]]
                break
            out.texts.append(st)
            total += st.words
        return out
