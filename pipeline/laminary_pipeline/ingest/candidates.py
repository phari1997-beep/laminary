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
- **Bucket rule** (``classify``, selector 1.2.0, DECISIONS 2026-10-02): the original language
  (P364) decides, not incidental country, filming or secondary-language links. English
  varieties (American, British, Australian English...) count as English. Wikidata gives a
  title's original languages as an unordered set, so there is no "first" language; when a
  title has several, its country of origin (P495) breaks the tie. Steps:

  0. No original language in Wikidata at all (some Indian series): the country of origin
     decides as before (India: tv:indian for a series, world for a film; South Korea:
     Korean; Japan: Japanese), else world.
  1. Map each original language to English, one of the six regional languages, or "other".
  2. No English or regional language: world.
  3. Exactly one of them and no "other" language: that one.
  4. Otherwise keep the languages whose home country is a country of origin (English: US,
     UK, Canada, Australia, Ireland, New Zealand; Hindi, Tamil, Malayalam, Telugu: India;
     Korean: South Korea; Japanese: Japan). One left: that one. English and a regional
     language both left (a co-production with an English-speaking country): English. Several
     regional languages left: the fixed order Tamil, Malayalam, Telugu, Hindi, Korean,
     Japanese.
  5. None left: world when the title has a country of origin (e.g. a French film listing
     English and Tamil); with no P495 at all, English if listed, else the fixed order.

  Series: Japanese goes to anime only for anime classes/genres (else world); the Indian
  languages go to tv:indian. Each row records the step as ``bucket_basis``.
- **Bucket overrides** (selector 1.3.0, DECISIONS 2026-10-02): a small hand-kept QID -> bucket
  table (``BUCKET_OVERRIDES``) for titles the language rule gets wrong, applied after the rule
  with ``bucket_basis: "override"``. ``classify`` and ``select`` both apply it, and ``select``
  puts every title in exactly one bucket, so an overridden title can't land in two pools.
  ``gather`` always fetches the listed QIDs, so a title reaches its bucket whichever pool query
  finds it (or none).
- **Within each cell** half the quota is taken purely by fame (sitelinks = number of Wikipedia
  language editions), and the rest round-robins across coarse genres, so every cell has
  well-known titles (gold-set material) and genre spread.
- **Gold seeds** (``data/config/gold_seed_titles.csv``) are forced in first, so the gold set can
  be drawn from the pilot.
- **Reserves**: ~30% extra per bucket, ranked, used by ``plots --backfill`` to replace titles
  that fail the 150-word rule without changing the mix.
- Excluded: documentaries, concert films, stand-up, reality/competition/talk/sketch shows,
  game, quiz, factual, educational and preschool edutainment shows, wrestling, and anthology
  series (no series-level story to annotate), by genre label plus a short hand-checked QID list
  (``NON_NARRATIVE_QIDS``) for edutainment Wikidata labels only as "children's television";
  titles with no (or several) TMDB ids in Wikidata, because LLM and gold records require one.
  Those are counted per bucket in the summary, since regional titles are the most likely to
  lack one.

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
    ENGLISH_LANGS,
    LANG,
    NAMED_LANGS,
    PoolQuery,
    Wikidata,
)

SELECTOR_VERSION = "1.3.0"  # 1.1.0: non-narrative series filter (DECISIONS 2026-10-01);
# 1.2.0: original language decides the bucket, English varieties count as English, English
# pools include the varieties (DECISIONS 2026-10-02); 1.3.0: bucket override table
# (DECISIONS 2026-10-02)
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
    # 1.1.0 (DECISIONS 2026-10-01): game, reality, educational and documentary-style shows.
    # Matched as substrings of the English genre labels, so each is specific enough not to hit
    # story genres ("travel" alone would match "time-travel fiction").
    "factual", "educational", "edutainment", "preschool", "children's music", "talent show",
    "quiz", "professional wrestling", "sports entertainment", "cooking show",
    "travel documentary", "lifestyle", "makeover", "dating show", "hidden camera",
    "infotainment", "instructional", "docuseries", "competition television",
)
# Non-narrative titles whose Wikidata genres don't say so (children's edutainment). Checked
# by hand; extend when a pilot review finds another.
NON_NARRATIVE_QIDS = {
    "Q3109770": "Zoboomafoo: children's wildlife edutainment",
    "Q41403": "Teletubbies: preschool edutainment without a series-level story",
}


def coarse_genre(genres: Iterable[str]) -> str:
    labels = [g.lower() for g in genres]
    for group, words in GENRE_GROUPS:
        if any(w in label for label in labels for w in words):
            return group
    return "other"


def excluded_reason(item: dict[str, Any]) -> str | None:
    if item.get("qid") in NON_NARRATIVE_QIDS:
        return "non_narrative:listed"
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


# Countries of origin (P495) that back each named language in the tie-break (step 4).
ENGLISH_COUNTRIES = {
    "Q30": "United States", "Q145": "United Kingdom", "Q16": "Canada", "Q408": "Australia",
    "Q27": "Ireland", "Q664": "New Zealand",
}
HOME_COUNTRIES: dict[str, frozenset[str]] = {
    "english": frozenset(ENGLISH_COUNTRIES),
    "hindi": frozenset({COUNTRY["india"]}),
    "tamil": frozenset({COUNTRY["india"]}),
    "malayalam": frozenset({COUNTRY["india"]}),
    "telugu": frozenset({COUNTRY["india"]}),
    "korean": frozenset({COUNTRY["south_korea"]}),
    "japanese": frozenset({COUNTRY["japan"]}),
}
REGIONAL_ORDER = ("tamil", "malayalam", "telugu", "hindi", "korean", "japanese")
INDIAN = frozenset({"tamil", "malayalam", "telugu", "hindi"})
NOT_ENGLISH_LABELS = frozenset({"Old English", "Middle English"})
_ENGLISH_LABEL = re.compile(r"[A-Z][\w .'-]* English")


def is_english(qid: str, label: str | None = None) -> bool:
    """English or one of its varieties: the listed QIDs, or an English label "<X> English"
    other than Old/Middle English."""
    if qid in ENGLISH_LANGS:
        return True
    return bool(label and label not in NOT_ENGLISH_LABELS and _ENGLISH_LABEL.fullmatch(label))


def language_groups(item: dict[str, Any]) -> tuple[set[str], bool]:
    """(named languages among the original languages, whether any other language is listed).
    Named: "english" (any variety) and the six regional languages."""
    labels = item.get("language_labels") or {}
    named: set[str] = set()
    other = False
    for qid in item.get("languages", []):
        name = LANG_NAME.get(qid) or ("english" if is_english(qid, labels.get(qid)) else None)
        if name:
            named.add(name)
        else:
            other = True
    return named, other


def _first_regional(names: Iterable[str]) -> str:
    return min(names, key=REGIONAL_ORDER.index)


def classify_language(item: dict[str, Any]) -> tuple[str | None, str]:
    """(the named language that decides the bucket, or None for world; the rule step used).
    See the module docstring, "Bucket rule"."""
    named, other = language_groups(item)
    countries = set(item.get("countries", []))
    if not item.get("languages"):
        # no original language in Wikidata at all (some Indian series): the country of origin
        # is the only evidence, used as before 1.2.0 (an Indian film without a language is
        # world: its language bucket is unknown)
        if COUNTRY["india"] in countries:
            tv = item.get("media_type") == "tv_series"
            return ("hindi", "no_language_country_only") if tv else (None, "no_language")
        for name in ("korean", "japanese"):
            if HOME_COUNTRIES[name] & countries:
                return name, "no_language_country_only"
        return None, "no_language"
    if not named:
        return None, "no_named_language"
    if len(named) == 1 and not other:
        return next(iter(named)), "only_language"
    home = {n for n in named if HOME_COUNTRIES[n] & countries}
    if len(home) == 1:
        return next(iter(home)), "country_of_origin"
    if "english" in home:
        return "english", "co_production_english"
    if home:
        return _first_regional(home), "country_of_origin_fixed_order"
    if countries:
        return None, "no_language_matches_country"
    if "english" in named:
        return "english", "no_country_english_listed"
    regional = named - {"english"}
    return _first_regional(regional), "no_country_fixed_order"


def bucket_for(item: dict[str, Any], language: str | None) -> str:
    tv = item["media_type"] == "tv_series"
    if language is None:
        return "tv:world" if tv else "film:world"
    if language == "english":
        return "tv:english" if tv else "film:english"
    if not tv:
        return f"film:{language}"
    if language in INDIAN:
        return "tv:indian"
    if language == "korean":
        return "tv:korean"
    anime = set(item.get("classes", [])) & set(ANIME_TV_CLASSES) or any(
        "anime" in g.lower() for g in item.get("genres", [])
    )
    return "tv:anime" if anime else "tv:world"


# Hand-kept bucket overrides (selector 1.3.0, DECISIONS 2026-10-02): QID -> bucket, for titles
# the language rule gets wrong. QIDs looked up in the cached candidate data
# (pipeline/data/pilot_candidates.jsonl, selector 1.1.0 run); the comment gives the title, year
# and the Wikidata original languages (P364) that misled the rule. Shogun (2024), Minari
# (Q65679599) and Letters from Iwo Jima (Q275187) are US productions and stay English: they
# are deliberately not listed.
BUCKET_OVERRIDES: dict[str, str] = {
    "Q13897247": "film:telugu",  # Baahubali: The Beginning (2015); Tamil + Telugu
    "Q21001674": "film:telugu",  # Baahubali 2: The Conclusion (2017); Telugu
    "Q3234794": "film:telugu",  # Eega (2012); Tamil + Telugu
    "Q65057760": "film:telugu",  # Ala Vaikunthapurramuloo (2020); Malayalam + Telugu
    "Q2962658": "film:hindi",  # Chennai Express (2013); Hindi + Tamil
    "Q330663": "film:hindi",  # My Name Is Khan (2010); Hindi + English, US/India/UAE
    "Q13409848": "tv:korean",  # The Heirs (2013); English + Korean, US/South Korea
    "Q718524": "film:world",  # Lust, Caution (2007); Hindi + English + Standard Chinese
}
BUCKET_BASIS_OVERRIDE = "override"


def override_for(item: dict[str, Any]) -> str | None:
    """The hand-kept bucket for this item, or None. An override for the wrong media type (a
    data error) is ignored rather than moving a film into a series bucket."""
    bucket = BUCKET_OVERRIDES.get(item.get("qid") or "")
    if bucket is None or BUCKET_BY_NAME[bucket].media_type != item.get("media_type"):
        return None
    return bucket


def classify(item: dict[str, Any]) -> str:
    """Bucket name for an item, from its type, original languages (P364) and, to break ties,
    its countries of origin (P495), unless ``BUCKET_OVERRIDES`` lists it. See the module
    docstring, "Bucket rule"."""
    return override_for(item) or bucket_for(item, classify_language(item)[0])


def classify_with_basis(item: dict[str, Any]) -> tuple[str, str]:
    """(bucket, bucket_basis): the override with basis "override", else the language rule
    and the step that decided it."""
    override = override_for(item)
    if override is not None:
        return override, BUCKET_BASIS_OVERRIDE
    language, basis = classify_language(item)
    return bucket_for(item, language), basis


def language_display(candidate: dict[str, Any]) -> str:
    """A candidate's primary language for reports: the bucket name for named languages
    ("english"), else the English Wikidata label with its QID ("German (Q188)"), else the QID."""
    lang = candidate.get("language") or "unknown"
    label = (candidate.get("language_labels") or {}).get(lang)
    return f"{label} ({lang})" if label else lang


def primary_language(item: dict[str, Any]) -> str:
    """The language that decided the bucket ("english", "korean", ...); for a world title its
    first original language QID (sorted), else "unknown". For an overridden title, the
    override bucket's language (Telugu for film:telugu; the first other language for a world
    bucket)."""
    override = override_for(item)
    if override is not None:
        region = BUCKET_BY_NAME[override].region
        language = region if region in HOME_COUNTRIES else None
    else:
        language = classify_language(item)[0]
    if language is not None:
        return language
    labels = item.get("language_labels") or {}
    for qid in item.get("languages", []):
        if qid not in LANG_NAME and not is_english(qid, labels.get(qid)):
            return qid
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
    not_named = ", ".join(f"wd:{q}" for q in (*NAMED_LANGS, *ENGLISH_LANGS[1:]))
    # English pools take every listed English variety (selector 1.2.0); world pools exclude
    # them, so English-language titles don't fill the world pools' limits.
    english = "VALUES ?l_ { " + " ".join(f"wd:{q}" for q in ENGLISH_LANGS) + " } " \
        "?item wdt:P364 ?l_ ."
    qs: list[PoolQuery] = []
    for key, y0, y1 in _decade_ranges(FILM_ENGLISH_DECADES, 1960):
        quota = FILM_ENGLISH_DECADES[key]
        qs.append(PoolQuery(f"film:english:{key}", "movie", english, y0, y1, 25, limit(quota)))
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
        qs.append(PoolQuery(f"tv:english:{key}", "tv_series", english,
                            y0 if y0 > 1870 else 1930, y1, 20, limit(quota)))
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
        # one bucket per title (overrides included), so no title is in two pools
        it["bucket"], it["bucket_basis"] = classify_with_basis(it)
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
        "series_status_basis": it.get("series_status_basis"),
        "number_of_seasons": it.get("number_of_seasons"),
        "tmdb_id": it["tmdb_id"],
        "tmdb_id_ambiguous": it["tmdb_id_ambiguous"],
        "imdb_id": it["imdb_id"],
        "bucket": bucket.name,
        "bucket_basis": it.get("bucket_basis"),
        "role": role,
        "bucket_rank": rank,
        "region": bucket.region,
        "language": it["language"],
        "decade": decade_key(it["year"], 1960 if it["media_type"] == "movie" else 1990),
        "genre": it["genre"],
        "genres": it["genres"],
        "languages": it["languages"],
        "language_labels": {
            q: label for q, label in it.get("language_labels", {}).items()
            if q in it["languages"]
        },
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
    qids: set[str] = set(BUCKET_OVERRIDES)  # always fetched, whichever pool finds them
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
