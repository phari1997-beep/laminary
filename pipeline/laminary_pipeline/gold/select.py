"""Pick ~100 gold titles from the pilot candidates.

Rules, in priority order:

1. **Eligible:** in the effective pilot (pilot rows, or reserves that replaced failed ones), has
   a TMDB id from Wikidata (``gold_label`` requires one), and, once plots are fetched, passed the
   150-word rule. Labelers read the same summary the model sees (DECISIONS 2026-09-26).
2. **Seeds first:** hand-picked well-known titles from ``data/config/gold_seed_titles.csv``,
   each with a guessed plot and arc. Guesses only steer balance; they're never shown to
   labelers and aren't labels.
3. **Balance:** greedy. Each pick fills the most-needed type (about 30% TV), then regional
   titles (at least 20 non-English), then the least-covered guessed plot, then the
   least-covered guessed arc (including "flat" stories, to test the fallback rule), then fame.
4. **Top up** from the most famous eligible non-seed titles if seeds run out.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any

DEFAULT_N = 100
TV_SHARE = 0.3
REGIONAL_MIN = 20


def select_gold(
    candidates: Sequence[dict[str, Any]],
    seeds: Sequence[dict[str, str]],
    *,
    plot_ok: Callable[[str], bool | None] = lambda qid: None,
    n: int = DEFAULT_N,
    tv_share: float = TV_SHARE,
    regional_min: int = REGIONAL_MIN,
) -> list[dict[str, Any]]:
    """``plot_ok(qid)`` is True/False once a plot file exists, None if not fetched yet."""
    guesses = {s["title"]: s for s in seeds}
    eligible = []
    for c in candidates:
        if not c.get("tmdb_id") or c.get("tmdb_id_ambiguous"):
            continue
        if plot_ok(c["qid"]) is False:
            continue
        seed = guesses.get(c.get("gold_seed_title") or "")
        eligible.append({
            "qid": c["qid"], "title": c["title"], "year": c["year"],
            "media_type": c["media_type"], "region": c["region"], "bucket": c["bucket"],
            "sitelinks": c["sitelinks"], "tmdb_id": c["tmdb_id"],
            "guessed_plot": seed["guessed_plot"] if seed else "unknown",
            "guessed_arc": seed["guessed_arc"] if seed else "unknown",
            "seed": seed is not None,
        })

    n_tv = round(n * tv_share)
    want = {"tv_series": n_tv, "movie": n - n_tv}
    chosen: list[dict[str, Any]] = []
    types: Counter[str] = Counter()
    plots: Counter[str] = Counter()
    arcs: Counter[str] = Counter()
    regional = 0

    def score(c: dict[str, Any]) -> tuple:
        is_regional = c["region"] != "english"
        return (
            0 if types[c["media_type"]] < want[c["media_type"]] else 1,
            0 if c["seed"] else 1,
            0 if (regional >= regional_min or is_regional) else 1,
            plots[c["guessed_plot"]] if c["seed"] else 0,
            arcs[c["guessed_arc"]] if c["seed"] else 0,
            -c["sitelinks"],
            int(c["qid"][1:]),
        )

    pool = {c["qid"]: c for c in eligible}
    while len(chosen) < n and pool:
        best = min(pool.values(), key=score)
        del pool[best["qid"]]
        chosen.append(best)
        types[best["media_type"]] += 1
        regional += best["region"] != "english"
        if best["seed"]:
            plots[best["guessed_plot"]] += 1
            arcs[best["guessed_arc"]] += 1
    return chosen


def summarize_gold(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    def count(key: str) -> dict[str, int]:
        return dict(sorted(Counter(str(r[key]) for r in rows).items()))

    return {
        "total": len(rows),
        "seeds": sum(1 for r in rows if r["seed"]),
        "by_media_type": count("media_type"),
        "by_region": count("region"),
        "by_guessed_plot": count("guessed_plot"),
        "by_guessed_arc": count("guessed_arc"),
    }
