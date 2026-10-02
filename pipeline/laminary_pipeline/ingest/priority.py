"""The priority series: big shows Hari named that must not drop out of the pilot.

DECISIONS 2026-10-02: they are forced into the pilot selection like gold seeds
(``candidates.select``, selector 1.3.1), always fetched by ``candidates.gather``, never replaced
by backfill, and the plots report counts one as passing only when it is in the effective pilot
(``plots.priority_report``). QIDs from the cached candidate data
(pipeline/data/pilot_candidates.jsonl).
"""

from __future__ import annotations

PRIORITY_SERIES: dict[str, str] = {
    "Q23733": "Seinfeld",
    "Q16290": "Star Trek: The Next Generation",
    "Q494244": "M*A*S*H",
    "Q751917": "Midsomer Murders",
    "Q23572": "Game of Thrones",
    "Q192837": "Sherlock",
    "Q4525": "NCIS",
    "Q485668": "Scrubs",
    "Q252118": "CID",
    "Q34316": "Doctor Who",
}
