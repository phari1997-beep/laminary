"""Build the 500-title pilot candidate list from Wikidata (CC0).

Rationale (also in the Phase 1 report and data/README.md):

- **350 films + 150 TV series** (series level only; seasons and episodes are different Wikidata
  classes and never enter the pool).
- **English-language titles get decade quotas** so the pilot isn't all 2010s blockbusters: the
  arc and plot mix changes across eras, and older titles test the prompt on different summary
  styles.
- **Regional and non-English buckets are fixed quotas** (Hindi, Tamil, Malayalam, Telugu,
  Korean, Japanese including anime, and a "world" bucket). The Phase 0 interviews asked for
  Tamil/Malayalam/Korean titles and anime (risk 6), and the launch isn't US-only
  (DECISIONS 2026-09-29). The pilot measures how often these titles have a 150-word English
  Wikipedia plot section.
- **Within each cell** half the quota is taken purely by fame (sitelinks = number of Wikipedia
  language editions), and the rest round-robins across coarse genres, so every cell has
  well-known titles (gold-set material) and genre spread.
- **Gold seeds** (``data/config/gold_seed_titles.csv``) are forced in first, so the gold set can
  be drawn from the pilot.
- **Reserves**: ~30% extra per bucket, ranked, used by ``plots --backfill`` to replace titles
  that fail the 150-word rule without changing the mix.
- Excluded: documentaries, concert films, stand-up, reality/competition/talk/sketch shows, and
  anthology series (no series-level story to annotate); titles with no (or several) TMDB ids in
  Wikidata, because LLM and gold records require one. Those are counted per bucket in the
  summary, since regional titles are the most likely to lack one.

All selection is deterministic: ties break on sitelinks, then QID.
"""

from __future__ import annotations

import csv
import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from laminary_pipeline.ingest.wikidata import (
    ANIME_TV_CLASSES,
    COUNTRY,
    LANG,
    NAMED_LANGS,
    PoolQuery,
    Wikidata,
)

SELECTOR_VERSION = "1.0.0"
RESERVE_RATIO = 0.3
FAME_SHARE = 0.5  # share of each cell taken purely by sitelinks before genre round-robin

LANG_NAME = {v: k for k, v in LANG.items()}


@dataclass(frozen=True)
class Bucket:
    name: str
    media_type: str
    quota: int
    region: str  # reporting label: english, hindi, tamil, ..., world
    decade_quotas: dict[str, int] = field(default_factory=dict)
    decade_floor: int = 1960  # years before this share one "pre-<floor>" cell


FILM_ENGLISH_DECADES = {
    "pre-1960": 20, "1960s": 15, "1970s": 25, "1980s": 30, "1990s": 35, "2000s": 35,
    "2010s": 40, "2020s": 20,
}
TV_ENGLISH_DECADES = {"pre-1990": 10, "1990s": 15, "2000s": 25, "2010s": 30, "2020s": 15}

BUCKETS: tuple[Bucket, ...] = (
    Bucket("film:english", "movie", 220, "english", FILM_ENGLISH_DECADES, 1960),
    Bucket("film:hindi", "movie", 20, "hindi"),
    Bucket("film:tamil", "movie", 20, "tamil"),
    Bucket("film:malayalam", "movie", 15, "malayalam"),
    Bucket("film:telugu", "movie", 5, "telugu"),
    Bucket("film:korean", "movie", 20, "korean"),
    Bucket("film:japanese", "movie", 25, "japanese"),
    Bucket("film:world", "movie", 25, "world"),
    Bucket("tv:english", "tv_series", 95, "english", TV_ENGLISH_DECADES, 1990),
    Bucket("tv:korean", "tv_series", 15, "korean"),
    Bucket("tv:anime", "tv_series", 20, "japanese"),
    Bucket("tv:indian", "tv_series", 8, "indian"),
    Bucket("tv:world", "tv_series", 12, "world"),
)
BUCKET_BY_NAME = {b.name: b for b in BUCKETS}
TARGET_TOTAL = sum(b.quota for b in BUCKETS)  # 500

# Coarse genre from Wikidata genre labels: first matching group wins.
GENRE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("animation", ("anime", "animated", "animation")),
    ("horror", ("horror", "slasher", "zombie")),
    ("science_fiction", ("science fiction", "sci-fi", "cyberpunk", "dystopian")),
    ("fantasy", ("fantasy", "fairy tale")),
    ("war", ("war film", "war drama", "war ")),
    ("western", ("western",)),
    ("musical", ("musical",)),
    ("crime", ("crime", "gangster", "heist", "police procedural", "legal")),
    ("thriller", ("thriller", "suspense", "spy")),
    ("mystery", ("mystery", "detective", "whodunit")),
    ("action", ("action", "superhero", "martial arts", "masala")),
    ("comedy", ("comedy", "sitcom", "satire", "parody")),
    ("romance", ("romance", "romantic")),
    ("adventure", ("adventure",)),
    ("drama", ("drama", "biographical", "melodrama", "coming-of-age")),
)
EXCLUDE_GENRE_WORDS = (
    "documentary", "concert", "stand-up", "reality", "game show", "talk show", "variety show",
    "sketch comedy", "anthology", "news", "docudrama series", "nature",
)


def coarse_genre(genres: Iterable[str]) -> str:
    labels = [g.lower() for g in genres]
    for group, words in GENRE_GROUPS:
        if any(w in label for label in labels for w in words):
            return group
    return "other"


def excluded_reason(item: dict[str, Any]) -> str | None:
    labels = " | ".join(g.lower() for g in item.get("genres", []))
    for word in EXCLUDE_GENRE_WORDS:
        if word in labels:
            return f"genre:{word}"
    if not item.get("enwiki_title"):
        return "no_enwiki_article"
    if not item.get("year"):
        return "no_year"
    # LLM and gold records require an integer tmdb_id (schema 1.0.0), taken from Wikidata.
    if item.get("tmdb_id_ambiguous"):
        return "tmdb_id_ambiguous"
    if not item.get("tmdb_id"):
        return "no_tmdb_id"
    return None


def decade_key(year: int, floor: int) -> str:
    if year < floor:
        return f"pre-{floor}"
    return f"{year // 10 * 10}s"


def classify(item: dict[str, Any]) -> str:
    """Bucket name for an item, from its type, original languages (P364) and countries (P495).
    Country wins for India/Korea/Japan so co-produced or partly English titles land in their
    regional bucket."""
    langs = set(item.get("languages", []))
    countries = set(item.get("countries", []))
    tv = item["media_type"] == "tv_series"
    indian_langs = [n for n in ("tamil", "malayalam", "telugu", "hindi") if LANG[n] in langs]
    if COUNTRY["india"] in countries or indian_langs:
        if tv:
            return "tv:indian"
        return f"film:{indian_langs[0]}" if indian_langs else "film:world"
    if COUNTRY["south_korea"] in countries or LANG["korean"] in langs:
        return "tv:korean" if tv else "film:korean"
    if COUNTRY["japan"] in countries or LANG["japanese"] in langs:
        if tv:
            anime = set(item.get("classes", [])) & set(ANIME_TV_CLASSES) or any(
                "anime" in g.lower() for g in item.get("genres", [])
            )
            return "tv:anime" if anime else "tv:world"
        return "film:japanese"
    if LANG["english"] in langs:
        return "tv:english" if tv else "film:english"
    return "tv:world" if tv else "film:world"


def primary_language(item: dict[str, Any]) -> str:
    for qid in item.get("languages", []):
        if qid in LANG_NAME:
            return LANG_NAME[qid]
    return item["languages"][0] if item.get("languages") else "unknown"


# ---------- pool queries ----------


def _decade_ranges(decades: dict[str, int], floor: int) -> list[tuple[str, int, int]]:
    out = []
    for key in decades:
        if key.startswith("pre-"):
            out.append((key, 1870, floor))
        else:
            start = int(key[:4])
            out.append((key, start, start + 10))
    return out


def pool_queries(scale: float = 3.0) -> list[PoolQuery]:
    """Pool queries per bucket (per decade for English). ``scale`` x quota rows each."""

    def limit(q: int) -> int:
        return max(60, int(q * scale))

    lang = LANG
    not_named = ", ".join(f"wd:{q}" for q in NAMED_LANGS)
    qs: list[PoolQuery] = []
    for key, y0, y1 in _decade_ranges(FILM_ENGLISH_DECADES, 1960):
        quota = FILM_ENGLISH_DECADES[key]
        where = f"?item wdt:P364 wd:{lang['english']} ."
        qs.append(PoolQuery(f"film:english:{key}", "movie", where, y0, y1, 25, limit(quota)))
    for name, floor in (("hindi", 15), ("tamil", 8), ("malayalam", 5), ("telugu", 10),
                        ("korean", 15), ("japanese", 20)):
        quota = BUCKET_BY_NAME[f"film:{name}"].quota
        qs.append(PoolQuery(f"film:{name}", "movie", f"?item wdt:P364 wd:{lang[name]} .",
                            1870, 2100, floor, limit(quota)))
    qs.append(PoolQuery("film:world", "movie",
                        f"?item wdt:P364 ?l_ . FILTER(?l_ NOT IN ({not_named}))",
                        1870, 2100, 50, limit(25)))
    for key, y0, y1 in _decade_ranges(TV_ENGLISH_DECADES, 1990):
        quota = TV_ENGLISH_DECADES[key]
        qs.append(PoolQuery(f"tv:english:{key}", "tv_series",
                            f"?item wdt:P364 wd:{lang['english']} .", y0 if y0 > 1870 else 1930,
                            y1, 20, limit(quota)))
    qs.append(PoolQuery("tv:korean", "tv_series", f"?item wdt:P364 wd:{lang['korean']} .",
                        1930, 2100, 10, limit(15)))
    qs.append(PoolQuery("tv:anime", "tv_series", f"?item wdt:P364 wd:{lang['japanese']} .",
                        1930, 2100, 15, limit(20), classes=ANIME_TV_CLASSES))
    qs.append(PoolQuery("tv:indian", "tv_series", f"?item wdt:P495 wd:{COUNTRY['india']} .",
                        1930, 2100, 3, limit(8)))
    qs.append(PoolQuery("tv:world", "tv_series",
                        f"?item wdt:P364 ?l_ . FILTER(?l_ NOT IN ({not_named}))",
                        1930, 2100, 20, limit(12)))
    return qs


# ---------- gold seeds ----------


def normalize_title(title: str) -> str:
    t = unicodedata.normalize("NFKD", title).casefold()
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.replace("&", "and").replace("×", "x")
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def load_seeds(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return [row for row in csv.DictReader(fh) if row.get("title") and
                not row["title"].startswith("#")]


def match_seed(seed: dict[str, str], items: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Best item for a seed: same normalized title, same type, year within 1."""
    want = normalize_title(seed["title"])
    year = int(seed["year"])

    def names(it: dict[str, Any]) -> set[str]:
        base = re.sub(r"\s*\(.*\)$", "", it.get("enwiki_title") or "")
        found = [it.get("title") or "", base, *it.get("seed_labels", [])]
        return {normalize_title(n) for n in found}

    hits = [
        it for it in items
        if it.get("year") and abs(it["year"] - year) <= 1
        and it["media_type"] == seed["media_type"]
        and want in names(it)
    ]
    if not hits:
        return None
    return sorted(hits, key=lambda it: (abs(it["year"] - year), -it["sitelinks"], it["qid"]))[0]


# ---------- selection ----------


def _rank_key(item: dict[str, Any]) -> tuple[int, int]:
    return (-item["sitelinks"], int(item["qid"][1:]))


def pick_cell(items: Sequence[dict[str, Any]], quota: int, must: set[str]) -> list[dict[str, Any]]:
    """Seeds first, then half by fame, then round-robin across coarse genres."""
    pool = sorted(items, key=_rank_key)
    picked = [it for it in pool if it["qid"] in must][:quota]
    taken = {it["qid"] for it in picked}
    fame_n = max(0, math.ceil(quota * FAME_SHARE) - len(picked))
    for it in pool:
        if fame_n <= 0 or len(picked) >= quota:
            break
        if it["qid"] not in taken:
            picked.append(it)
            taken.add(it["qid"])
            fame_n -= 1
    by_genre: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for it in pool:
        if it["qid"] not in taken:
            by_genre[it["genre"]].append(it)
    have = Counter(it["genre"] for it in picked)
    while len(picked) < quota and any(by_genre.values()):
        # Least-represented genre first; ties by the fame of its best remaining title.
        genre = min(
            (g for g, lst in by_genre.items() if lst),
            key=lambda g: (have[g], _rank_key(by_genre[g][0])),
        )
        it = by_genre[genre].pop(0)
        picked.append(it)
        have[genre] += 1
    return picked


def select_bucket(
    bucket: Bucket, items: Sequence[dict[str, Any]], must: set[str], reserve_ratio: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pool = sorted(items, key=_rank_key)
    if bucket.decade_quotas:
        picked: list[dict[str, Any]] = []
        for key, quota in bucket.decade_quotas.items():
            cell = [it for it in pool if decade_key(it["year"], bucket.decade_floor) == key]
            picked += pick_cell(cell, quota, must)
        if len(picked) < bucket.quota:  # a thin decade: top up from the whole bucket
            taken = {it["qid"] for it in picked}
            rest = [it for it in pool if it["qid"] not in taken]
            picked += pick_cell(rest, bucket.quota - len(picked), must)
    else:
        picked = pick_cell(pool, bucket.quota, must)
    taken = {it["qid"] for it in picked}
    n_reserve = math.ceil(bucket.quota * reserve_ratio)
    reserves = [it for it in pool if it["qid"] not in taken][:n_reserve]
    return picked, reserves


@dataclass
class Selection:
    rows: list[dict[str, Any]]
    excluded: Counter[str]
    seeds_missing: list[dict[str, str]]
    pool_size: int


def select(
    items: dict[str, dict[str, Any]],
    seeds: Sequence[dict[str, str]] = (),
    *,
    reserve_ratio: float = RESERVE_RATIO,
    retrieved_at: str | None = None,
) -> Selection:
    """Pure selection over detailed items (QID -> detail dict from ``Wikidata.details``)."""
    excluded: Counter[str] = Counter()
    eligible: list[dict[str, Any]] = []
    for it in items.values():
        reason = excluded_reason(it)
        if reason:
            if reason.startswith(("no_tmdb", "tmdb")):  # coverage gap: report by bucket
                reason = f"{reason}:{classify(it)}"
            excluded[reason] += 1
            continue
        it = dict(it)
        it["genre"] = coarse_genre(it["genres"])
        it["bucket"] = classify(it)
        it["language"] = primary_language(it)
        eligible.append(it)

    seed_titles: dict[str, str] = {}
    missing: list[dict[str, str]] = []
    for seed in seeds:
        hit = match_seed(seed, eligible)
        if hit:
            seed_titles.setdefault(hit["qid"], seed["title"])
        else:
            missing.append(seed)
    seed_qids = set(seed_titles)

    by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for it in eligible:
        by_bucket[it["bucket"]].append(it)

    rows: list[dict[str, Any]] = []
    for bucket in BUCKETS:
        picked, reserves = select_bucket(bucket, by_bucket[bucket.name], seed_qids, reserve_ratio)
        for role, group in (("pilot", picked), ("reserve", reserves)):
            for rank, it in enumerate(group, start=1):
                rows.append(_row(it, bucket, role, rank, seed_titles.get(it["qid"]), retrieved_at))
    return Selection(rows, excluded, missing, len(eligible))


def _row(
    it: dict[str, Any],
    bucket: Bucket,
    role: str,
    rank: int,
    seed_title: str | None,
    retrieved_at: str | None,
) -> dict[str, Any]:
    return {
        "qid": it["qid"],
        "title": it["title"],
        "enwiki_title": it["enwiki_title"],
        "year": it["year"],
        "end_year": it["end_year"],
        "media_type": it["media_type"],
        "series_status": it["series_status"],
        "tmdb_id": it["tmdb_id"],
        "tmdb_id_ambiguous": it["tmdb_id_ambiguous"],
        "imdb_id": it["imdb_id"],
        "bucket": bucket.name,
        "role": role,
        "bucket_rank": rank,
        "region": bucket.region,
        "language": it["language"],
        "decade": decade_key(it["year"], 1960 if it["media_type"] == "movie" else 1990),
        "genre": it["genre"],
        "genres": it["genres"],
        "languages": it["languages"],
        "countries": it["countries"],
        "sitelinks": it["sitelinks"],
        "gold_seed": seed_title is not None,
        "gold_seed_title": seed_title,
        "source": {"kind": "wikidata_sparql", "license": "CC0-1.0", "retrieved_at": retrieved_at},
        "selector_version": SELECTOR_VERSION,
    }


def interleave_limit(rows: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """First ``limit`` pilot rows, taken round-robin across buckets so the mix survives.
    Reserves are dropped when limiting."""
    by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["role"] == "pilot":
            by_bucket[r["bucket"]].append(r)
    out: list[dict[str, Any]] = []
    i = 0
    while len(out) < limit and any(i < len(v) for v in by_bucket.values()):
        for b in BUCKETS:
            lst = by_bucket.get(b.name, [])
            if i < len(lst) and len(out) < limit:
                out.append(lst[i])
        i += 1
    return out


# ---------- fetching ----------


def gather(wd: Wikidata, seeds: Sequence[dict[str, str]]) -> dict[str, dict[str, Any]]:
    """Run pool and seed queries, then fetch details for every QID found."""
    qids: set[str] = set()
    for q in pool_queries():
        qids.update(r["qid"] for r in wd.pool(q))
    seed_labels: dict[str, list[str]] = defaultdict(list)
    for hit in wd.seeds([s["title"] for s in seeds]):
        qids.add(hit["qid"])
        if hit["seed_label"]:
            seed_labels[hit["qid"]].append(hit["seed_label"])
    items = wd.details(sorted(qids))
    for qid, labels in seed_labels.items():
        if qid in items:
            items[qid]["seed_labels"] = sorted(set(labels))
    return items


def summarize(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    pilot = [r for r in rows if r["role"] == "pilot"]

    def count(key: str) -> dict[str, int]:
        return dict(sorted(Counter(str(r[key]) for r in pilot).items()))

    return {
        "pilot": len(pilot),
        "reserve": sum(1 for r in rows if r["role"] == "reserve"),
        "by_media_type": count("media_type"),
        "by_bucket": count("bucket"),
        "by_region": count("region"),
        "by_decade": count("decade"),
        "by_genre": count("genre"),
        "gold_seeds_in_pilot": sum(1 for r in pilot if r["gold_seed"]),
        "with_tmdb_id": sum(1 for r in pilot if r["tmdb_id"]),
        "with_imdb_id": sum(1 for r in pilot if r["imdb_id"]),
    }
