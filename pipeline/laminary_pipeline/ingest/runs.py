"""Per-title series run rules (fetcher 1.5.1): which numbering run of a series to use.

A few long-running series have two numbering runs on English Wikipedia that both start at 1, so
their season pages and episode-list headings collide. A rule here, keyed by the series' QID,
says which run the season-article prose and the episode-table fallback (``seasons.py``,
``episodes.py``) may use. It is a small hand-kept table, not a heuristic: a series without an
entry is handled exactly as before.

**Doctor Who** (Q34316, DECISIONS 2026-10-02, Hari: "revival only"): the classic run is
numbered "Season 1–26" (1963–1989) and the 2005 revival "Series 1" on, including series 14–15
of the 2023 relaunch (DECISIONS 2026-10-02: total 15). The rule
``doctor_who_revival`` uses the revival only:

- season pages: only "Doctor Who series N" / "Doctor Who (series N)". Every "Doctor Who
  season N" / "(season N)" page is ignored before verification, whichever run it belongs to;
- episode-list pages: only ones split by a year range starting in 2005 or later ("List of
  Doctor Who episodes (2005–present)"). The classic "(1963–1989)" page, the plain
  "List of Doctor Who episodes" and season-range or part splits are ignored, since they could
  hold either run;
- list-page headings: only "Series N". A "Season N" heading on an allowed page is skipped;
- the "of N" total counts revival series only: the verified "series" pages and allowed
  "Series N" headings. Wikidata's P2437 is not used, since it may count both runs.

**The 2023 relaunch** (Disney co-production, 2024 on). On English Wikipedia (main article,
revision 1377232352) its seasons are "Doctor Who series 14" and "series 15", which continue
the revival numbering, so the rule keeps them like any other revival series (DECISIONS
2026-10-02, Hari approved: the total is 15). If Wikipedia
renames them so the numbering restarts, they drop out without any code change:
"Doctor Who season 1 (2024)" is not a season-page title at all, and any "season N" page is
ignored; a "Season 1" heading on the 2005 list page is skipped by the heading rule; a
"Series 1" heading after "Series 13" stops the page (seasons must rise, ``episodes.py``); and
a later list page such as "(2023–present)" restarting at Series 1 is not used (seasons must
keep rising across pages). Those seasons are out of scope for now and are recorded as skipped.

Wikidata verification (P179/P361 with the series QID, ordinals) is unchanged: an allowed page
is still used only when its item is stated as part of the series.

**Series ordinals** (fetcher 1.5.4). Wikidata numbers Doctor Who's seasons in one run across
both eras: "Doctor Who series 1" is stated P179 Q34316 with series ordinal (P1545) 27, series 15
with 41 (cached SPARQL, 2026-10-02), after the classic seasons 1–26. The plain ordinal check
(ordinal must equal the number in the title) rejected every revival page. ``ordinal_offset``
(26 for Doctor Who) states that convention: a revival "series N" page passes only when its
ordinal is exactly N + 26 (or it has none), so the check is as strict as before, just against
Wikidata's numbering. A page whose ordinal is anything else is still skipped.

**CID** (Q252118, DECISIONS 2026-10-02, Hari's option "a"; fetcher 1.5.5). Its four verified
episode-list pages ("List of CID episodes: 1998–2009", "2010–2014", "2015–2018",
"2024–present") have no season headings: each year is an h2 ("1998", "1999" ... "2025") over
its episode table. The rule ``cid_years`` (numbering "year") treats each year heading as one
"season" numbered by its year, in year order across the split pages (which sort by their start
year), with the same episode-table caps and episode-boundary stop. Every episode-list form is
allowed (plain or split); season pages and "Season N" headings are ignored (CID has none).
Sources then carry ``year`` instead of ``season``, episode markers read "1998E5", and the
coverage line states years: "Summary covers 1998–1999 of 1998–2025." (``seasons.year_coverage``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

NUMBERINGS = ("season", "series", "year")
YEAR = "year"


@dataclass(frozen=True)
class SeriesRunRule:
    name: str  # recorded in the plot file as ``series_run_rule``
    # "season" or "series": the word the run's page titles and headings use; "year": list-page
    # year headings ("1998") are the run's units and season pages are not used
    numbering: str
    list_pages_from: int  # episode-list pages used: split by a year range starting this year+
    description: str
    # Wikidata's series ordinal (P179 P1545) for the run's season N is N + ordinal_offset
    ordinal_offset: int = 0
    # the fetcher version that brought the rule's current form: files from older fetchers are
    # fetched again (``plots.needs_fetch``)
    since: tuple[int, int, int] = (1, 5, 1)

    def expected_ordinal(self, season: int) -> int:
        return season + self.ordinal_offset

    def season_page_ok(self, numbering: str | None) -> bool:
        """A season page titled with this numbering word ("series" in "<X> series 4")."""
        return numbering == self.numbering

    def list_page_ok(self, order: tuple[int, int] | None) -> bool:
        """An episode-list page, by its ``seasons.episode_list_order`` key: only a page split
        by a year range (kind 1) starting in or after ``list_pages_from``; with numbering
        "year", any episode-list page of the series."""
        if self.numbering == YEAR:
            return order is not None
        return order is not None and order[0] == 1 and order[1] >= self.list_pages_from

    def heading_ok(self, numbering: str | None) -> bool:
        """An episode-list season heading ("Series 3 (2007)") in this run's numbering."""
        return numbering == self.numbering

    def season_page_reason(self) -> str:
        if self.numbering == YEAR:
            return (f"outside the {self.name} run: only year headings on episode-list pages "
                    "are used (series run rule)")
        return (f"outside the {self.name} run: only '{self.numbering} N' season pages are "
                "used (series run rule)")

    def list_page_reason(self) -> str:
        return (f"outside the {self.name} run: only episode-list pages split by a year range "
                f"from {self.list_pages_from} are used (series run rule)")

    def heading_reason(self) -> str:
        if self.numbering == YEAR:
            return (f"outside the {self.name} run: only year headings are used (series run "
                    "rule)")
        return (f"outside the {self.name} run: only '{self.numbering} N' headings are used "
                "(series run rule)")

    def record(self) -> dict[str, Any]:
        return {"rule": self.name, "numbering": self.numbering,
                "list_pages_from": self.list_pages_from, "description": self.description,
                "ordinal_offset": self.ordinal_offset}


SERIES_RUN_RULES: dict[str, SeriesRunRule] = {
    "Q34316": SeriesRunRule(
        name="doctor_who_revival",
        numbering="series",
        list_pages_from=2005,
        description="Doctor Who: the 2005 revival only ('Series N' pages and headings); the "
        "classic 1963-1989 'Season N' run is ignored (DECISIONS 2026-10-02)",
        ordinal_offset=26,  # Wikidata: series 1 is the 27th season of Q34316
        since=(1, 5, 4),
    ),
    "Q252118": SeriesRunRule(
        name="cid_years",
        numbering=YEAR,
        list_pages_from=0,  # not used with numbering "year": every episode-list page counts
        description="CID: each year heading on its verified episode-list pages is one "
        "'season', in year order; the coverage line states years (DECISIONS 2026-10-02)",
        # 1.5.5 brought the rule; 1.5.6 reads years from the heading path; 1.5.7 skips a
        # specials heading anywhere above a year
        since=(1, 5, 7),
    ),
}


def run_rule_for(qid: str) -> SeriesRunRule | None:
    return SERIES_RUN_RULES.get(qid)
