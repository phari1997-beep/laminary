"""Wikidata (CC0) access: SPARQL on query.wikidata.org.

Two-stage querying keeps each request light enough for the 60-second WDQS limit:

- **Pool queries** (one per bucket, and per decade for the big English buckets) return only
  ``?item``, ``?sitelinks`` and the first release year, ordered by sitelinks (the number of
  Wikipedia language editions with an article: our "well-known" proxy) with a LIMIT.
- **Detail queries** fetch labels, enwiki title, TMDB/IMDb ids, languages, countries, genres,
  classes, end year and number of seasons (P2437, series) for a bounded list of QIDs
  (``VALUES``), in chunks.
- **Seed lookup** finds the hand-picked gold seed titles by exact English label.

TMDB ids (P4947 movie, P4983 TV) are read from Wikidata as plain identifiers. No TMDB API is
called (docs/NARRATIVE_SCHEMA.md section 1).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from laminary_pipeline.ingest.http import HttpClient

SPARQL_URL = "https://query.wikidata.org/sparql"
POOL_TTL = 30 * 24 * 3600  # candidate pools don't need to be fresher than a month
DETAIL_CHUNK = 150

# Item classes (P31). Checked against Wikidata usage; verify with a live run (report).
# Films: film, animated film, anime film, animated feature film.
FILM_CLASSES = ("Q11424", "Q202866", "Q20650540", "Q29168811")
# Series: television series, animated series, anime series, miniseries, web series.
TV_CLASSES = ("Q5398426", "Q581714", "Q63952888", "Q1259759", "Q526877")
ANIME_TV_CLASSES = ("Q63952888", "Q581714")

LANG = {
    "english": "Q1860", "hindi": "Q1568", "tamil": "Q5885", "malayalam": "Q36236",
    "telugu": "Q8097", "korean": "Q9176", "japanese": "Q5287",
}
# Varieties of English that Wikidata uses as an original language (P364), all counted as
# English (DECISIONS 2026-10-02). These three were seen in the pilot's detail data with these
# labels; other varieties are recognised by their English label in ``candidates`` (a label
# "<X> English", except Old and Middle English, which are other languages).
ENGLISH_VARIANTS = {
    "Q7976": "American English", "Q7979": "British English", "Q44679": "Australian English",
}
ENGLISH_LANGS = (LANG["english"], *ENGLISH_VARIANTS)
COUNTRY = {"india": "Q668", "south_korea": "Q884", "japan": "Q17"}
NAMED_LANGS = tuple(LANG.values())

PREFIXES = """PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX wdno: <http://www.wikidata.org/prop/novalue/>
PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX schema: <http://schema.org/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
"""


def _values(var: str, qids: Iterable[str]) -> str:
    return f"VALUES ?{var} {{ {' '.join('wd:' + q for q in qids)} }}"


def date_pattern(media_type: str) -> str:
    # Series usually carry start time (P580); films carry publication date (P577).
    return "(wdt:P580|wdt:P577)" if media_type == "tv_series" else "wdt:P577"


@dataclass(frozen=True)
class PoolQuery:
    """One pool query: items of a class set, a filter, a year range and a sitelink floor."""

    query_id: str
    media_type: str
    where: str
    year_from: int
    year_to: int  # exclusive
    min_sitelinks: int
    limit: int
    classes: Sequence[str] = ()

    def sparql(self) -> str:
        classes = self.classes or (TV_CLASSES if self.media_type == "tv_series" else FILM_CLASSES)
        return f"""{PREFIXES}
# laminary pool query: {self.query_id}
SELECT ?item ?sitelinks (MIN(?year_) AS ?year) WHERE {{
  {_values("class_", classes)}
  ?item wdt:P31 ?class_ .
  {self.where}
  ?item wikibase:sitelinks ?sitelinks .
  FILTER(?sitelinks >= {self.min_sitelinks})
  ?article schema:about ?item ;
           schema:isPartOf <https://en.wikipedia.org/> .
  ?item {date_pattern(self.media_type)} ?date_ .
  BIND(YEAR(?date_) AS ?year_)
}}
GROUP BY ?item ?sitelinks
HAVING (MIN(?year_) >= {self.year_from} && MIN(?year_) < {self.year_to})
ORDER BY DESC(?sitelinks) ?item
LIMIT {self.limit}
"""


def detail_sparql(qids: Sequence[str]) -> str:
    return f"""{PREFIXES}
# laminary detail query ({len(qids)} items)
SELECT ?item
  (SAMPLE(?label_) AS ?label)
  (SAMPLE(?articleName_) AS ?enwiki_title)
  (SAMPLE(?sitelinks_) AS ?sitelinks)
  (MIN(?year_) AS ?year)
  (MAX(?endYear_) AS ?end_year)
  (MAX(?noEnd_) AS ?no_end)
  (MAX(?seasons_) AS ?number_of_seasons)
  (GROUP_CONCAT(DISTINCT ?tmdbMovie_; separator="|") AS ?tmdb_movie)
  (GROUP_CONCAT(DISTINCT ?tmdbTv_; separator="|") AS ?tmdb_tv)
  (GROUP_CONCAT(DISTINCT ?imdb_; separator="|") AS ?imdb)
  (GROUP_CONCAT(DISTINCT STRAFTER(STR(?class_), "entity/"); separator="|") AS ?classes)
  (GROUP_CONCAT(DISTINCT STRAFTER(STR(?lang_), "entity/"); separator="|") AS ?languages)
  (GROUP_CONCAT(DISTINCT CONCAT(STRAFTER(STR(?lang_), "entity/"), "=", ?langLabel_);
                separator="|") AS ?language_labels)
  (GROUP_CONCAT(DISTINCT STRAFTER(STR(?country_), "entity/"); separator="|") AS ?countries)
  (GROUP_CONCAT(DISTINCT ?genreLabel_; separator="|") AS ?genres)
WHERE {{
  {_values("item", qids)}
  ?item wdt:P31 ?class_ .
  ?item wikibase:sitelinks ?sitelinks_ .
  OPTIONAL {{ ?item rdfs:label ?label_ . FILTER(LANG(?label_) = "en") }}
  OPTIONAL {{ ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> ;
                       schema:name ?articleName_ . }}
  OPTIONAL {{ ?item (wdt:P577|wdt:P580) ?date_ . BIND(YEAR(?date_) AS ?year_) }}
  OPTIONAL {{ ?item wdt:P582 ?end_ . BIND(YEAR(?end_) AS ?endYear_) }}
  OPTIONAL {{ ?item a wdno:P582 . BIND(1 AS ?noEnd_) }}
  OPTIONAL {{ ?item wdt:P2437 ?seasons_ . }}
  OPTIONAL {{ ?item wdt:P4947 ?tmdbMovie_ . }}
  OPTIONAL {{ ?item wdt:P4983 ?tmdbTv_ . }}
  OPTIONAL {{ ?item wdt:P345 ?imdb_ . }}
  OPTIONAL {{ ?item wdt:P364 ?lang_ .
             OPTIONAL {{ ?lang_ rdfs:label ?langLabel_ . FILTER(LANG(?langLabel_) = "en") }} }}
  OPTIONAL {{ ?item wdt:P495 ?country_ . }}
  OPTIONAL {{ ?item wdt:P136 ?genre_ . ?genre_ rdfs:label ?genreLabel_ .
             FILTER(LANG(?genreLabel_) = "en") }}
}}
GROUP BY ?item
"""


def _escape_literal(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def seed_sparql(labels: Sequence[str]) -> str:
    values = " ".join(f'"{_escape_literal(label)}"@en' for label in sorted(set(labels)))
    classes = FILM_CLASSES + TV_CLASSES
    return f"""{PREFIXES}
# laminary gold-seed lookup ({len(set(labels))} labels)
SELECT DISTINCT ?item ?sitelinks ?seed_label WHERE {{
  VALUES ?seed_label {{ {values} }}
  {{ ?item rdfs:label ?seed_label . }} UNION {{ ?item skos:altLabel ?seed_label . }}
  {_values("class_", classes)}
  ?item wdt:P31 ?class_ .
  ?item wikibase:sitelinks ?sitelinks .
  ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> .
}}
"""


# ---------- result parsing ----------


def _value(binding: dict[str, Any], name: str) -> str | None:
    cell = binding.get(name)
    if not cell:
        return None
    value = cell.get("value")
    return value if value not in (None, "") else None


def qid_from_uri(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def _pairs(value: str | None) -> dict[str, str]:
    """'Q188=German|Q150=French' -> {'Q188': 'German', 'Q150': 'French'}."""
    out = {}
    for item in _split(value):
        key, sep, label = item.partition("=")
        if sep and key and label:
            out[key] = label
    return out


def _split(value: str | None) -> list[str]:
    return sorted({v for v in (value or "").split("|") if v})


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def parse_pool(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for b in data["results"]["bindings"]:
        rows.append(
            {
                "qid": qid_from_uri(_value(b, "item") or ""),
                "sitelinks": _int(_value(b, "sitelinks")) or 0,
                "year": _int(_value(b, "year")),
            }
        )
    return rows


def parse_seed_hits(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "qid": qid_from_uri(_value(b, "item") or ""),
            "sitelinks": _int(_value(b, "sitelinks")) or 0,
            "seed_label": _value(b, "seed_label"),
        }
        for b in data["results"]["bindings"]
    ]


def season_count(value: str | None) -> int | None:
    """Wikidata's number of seasons (P2437) as a whole number from 1 to 100, else None (a
    quantity like "7.5" or "0" is not a usable season count)."""
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if not number.is_integer() or not 1 <= number <= 100:
        return None
    return int(number)


def _single_int_id(values: list[str]) -> tuple[int | None, bool]:
    """One TMDB id, or None. Several distinct values -> None and flagged ambiguous."""
    ints = sorted({i for i in (_int(v) for v in values) if i and i > 0})
    if len(ints) == 1:
        return ints[0], False
    return None, len(ints) > 1


# Series status (narrative schema 1.1.0, DECISIONS 2026-09-30). Wikidata often lacks an end
# date (P582) for series that have ended, so a missing P582 means "unknown", not "ongoing".
# Evidence that counts:
#   ended:   an end time (P582) with a year.
#   ongoing: an explicit "no value" end time (wdno:P582), i.e. an editor asserted it has none.
#   unknown: neither, or both (conflicting statements).
# A recent season start date is not evidence of "ongoing": it can't tell a final season apart.
SERIES_STATUS_BASIS = ("P582", "P582_novalue", "none", "conflict")


def series_status(is_tv: bool, end_year: int | None, no_end: bool) -> tuple[str | None, str | None]:
    """(series_status, basis) for a candidate; (None, None) for films."""
    if not is_tv:
        return None, None
    if end_year and no_end:
        return "unknown", "conflict"
    if end_year:
        return "ended", "P582"
    if no_end:
        return "ongoing", "P582_novalue"
    return "unknown", "none"


def effective_series_status(candidate: dict[str, Any]) -> str | None:
    """The status to put in a record. Candidates written before ingest recorded
    ``series_status_basis`` called every series without P582 "ongoing"; those read as
    "unknown" (their "ended" stands: it came from P582)."""
    status = candidate.get("series_status")
    if candidate.get("media_type") != "tv_series":
        return status
    if candidate.get("series_status_basis") is not None:
        return status
    return "ended" if status == "ended" else "unknown"


def parse_details(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for b in data["results"]["bindings"]:
        qid = qid_from_uri(_value(b, "item") or "")
        classes = _split(_value(b, "classes"))
        is_tv = any(c in TV_CLASSES for c in classes) and not any(
            c in FILM_CLASSES for c in classes
        )
        media_type = "tv_series" if is_tv else "movie"
        id_values = _split(_value(b, "tmdb_tv" if is_tv else "tmdb_movie"))
        tmdb_id, ambiguous = _single_int_id(id_values)
        imdb = _split(_value(b, "imdb"))
        end_year = _int(_value(b, "end_year"))
        status, basis = series_status(is_tv, end_year, _int(_value(b, "no_end")) == 1)
        out[qid] = {
            "qid": qid,
            "title": _value(b, "label") or _value(b, "enwiki_title"),
            "enwiki_title": _value(b, "enwiki_title"),
            "year": _int(_value(b, "year")),
            "end_year": end_year,
            "media_type": media_type,
            "series_status": status,
            "series_status_basis": basis,
            "number_of_seasons": season_count(_value(b, "number_of_seasons")) if is_tv else None,
            "tmdb_id": tmdb_id,
            "tmdb_id_ambiguous": ambiguous,
            "imdb_id": imdb[0] if len(imdb) == 1 else None,
            "sitelinks": _int(_value(b, "sitelinks")) or 0,
            "classes": classes,
            "languages": _split(_value(b, "languages")),
            "language_labels": _pairs(_value(b, "language_labels")),
            "countries": _split(_value(b, "countries")),
            "genres": _split(_value(b, "genres")),
        }
    return out


class Wikidata:
    def __init__(self, client: HttpClient, *, cache_ttl: float | None = POOL_TTL) -> None:
        self.client = client
        self.cache_ttl = cache_ttl

    def sparql(self, query: str) -> dict[str, Any]:
        return self.client.post_form_json(
            SPARQL_URL,
            {"query": query, "format": "json"},
            headers={"Accept": "application/sparql-results+json"},
            cache_ttl=self.cache_ttl,
        )

    def pool(self, q: PoolQuery) -> list[dict[str, Any]]:
        return parse_pool(self.sparql(q.sparql()))

    def seeds(self, labels: Sequence[str]) -> list[dict[str, Any]]:
        if not labels:
            return []
        return parse_seed_hits(self.sparql(seed_sparql(labels)))

    def details(self, qids: Sequence[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        ordered = sorted(set(qids), key=lambda q: int(q[1:]))
        for i in range(0, len(ordered), DETAIL_CHUNK):
            out.update(parse_details(self.sparql(detail_sparql(ordered[i : i + DETAIL_CHUNK]))))
        return out
