"""Candidate selection: quotas, mix, seeds, exclusions, determinism, SPARQL rendering."""

from __future__ import annotations

import itertools
from collections import Counter
from typing import Any

import pytest
from wikimedia_fake import FakeWikimedia

from laminary_pipeline.ingest import candidates as c
from laminary_pipeline.ingest.http import HttpClient
from laminary_pipeline.ingest.wikidata import (
    LANG,
    PoolQuery,
    Wikidata,
    detail_sparql,
    effective_series_status,
    parse_details,
    seed_sparql,
    series_status,
)

GENRES = ["drama film", "comedy film", "horror film", "science fiction film", "crime film",
          "romance film", "animated film", "western film"]
_ids = itertools.count(1000)


def item(media_type: str, lang: str, year: int, sitelinks: int, genre: str,
         country: str = "Q30", classes: list[str] | None = None, title: str | None = None,
         tmdb: int | None = 1) -> dict[str, Any]:
    qid = f"Q{next(_ids)}"
    tv = media_type == "tv_series"
    return {
        "qid": qid, "title": title or f"Title {qid}", "enwiki_title": title or f"Title {qid}",
        "year": year, "end_year": None, "media_type": media_type,
        "series_status": "ongoing" if tv else None, "tmdb_id": tmdb, "tmdb_id_ambiguous": False,
        "imdb_id": None, "sitelinks": sitelinks,
        "classes": classes or (["Q5398426"] if tv else ["Q11424"]),
        "languages": [LANG[lang]] if lang in LANG else [lang], "countries": [country],
        "genres": [genre],
    }


def big_pool() -> dict[str, dict[str, Any]]:
    """Enough synthetic items to fill every bucket, with spread across decades and genres."""
    items: list[dict[str, Any]] = []
    for year in range(1930, 2026):
        for k in range(8):
            items.append(item("movie", "english", year, 20 + (year * 7 + k * 13) % 90,
                              GENRES[(year + k) % len(GENRES)]))
    for year in range(1955, 2026):
        for k in range(4):
            genre = GENRES[(year + k) % len(GENRES)].replace("film", "television series")
            items.append(item("tv_series", "english", year, 15 + (year * 3 + k) % 80, genre))
    regional = [("hindi", "Q668"), ("tamil", "Q668"), ("malayalam", "Q668"), ("telugu", "Q668"),
                ("korean", "Q884"), ("japanese", "Q17"), ("Q150", "Q142")]  # Q150 French
    for lang, country in regional:
        for k in range(60):
            items.append(item("movie", lang, 1960 + k % 60, 5 + k, GENRES[k % len(GENRES)],
                              country=country))
    for k in range(40):
        items.append(item("tv_series", "korean", 2000 + k % 25, 10 + k, "drama television series",
                          country="Q884"))
        items.append(item("tv_series", "japanese", 1990 + k % 35, 10 + k, "anime",
                          country="Q17", classes=["Q63952888"]))
        items.append(item("tv_series", "hindi", 2010 + k % 15, 3 + k, "crime drama",
                          country="Q668"))
        items.append(item("tv_series", "Q188", 2000 + k % 25, 20 + k, "thriller television series",
                          country="Q183"))  # German
    return {it["qid"]: it for it in items}


@pytest.fixture(scope="module")
def selection() -> c.Selection:
    return c.select(big_pool(), retrieved_at="2026-09-30T00:00:00Z")


def test_targets_are_500_titles_350_films_150_series() -> None:
    assert c.TARGET_TOTAL == 500
    assert sum(b.quota for b in c.BUCKETS if b.media_type == "movie") == 350
    assert sum(b.quota for b in c.BUCKETS if b.media_type == "tv_series") == 150
    for b in c.BUCKETS:
        if b.decade_quotas:
            assert sum(b.decade_quotas.values()) == b.quota


def test_selection_fills_every_bucket_and_decade(selection: c.Selection) -> None:
    pilot = [r for r in selection.rows if r["role"] == "pilot"]
    assert len(pilot) == 500
    assert len({r["qid"] for r in selection.rows}) == len(selection.rows)  # no duplicates
    by_bucket = Counter(r["bucket"] for r in pilot)
    assert by_bucket == {b.name: b.quota for b in c.BUCKETS}
    eng_films = Counter(r["decade"] for r in pilot if r["bucket"] == "film:english")
    assert eng_films == c.FILM_ENGLISH_DECADES
    eng_tv = Counter(r["decade"] for r in pilot if r["bucket"] == "tv:english")
    assert eng_tv == c.TV_ENGLISH_DECADES
    summary = c.summarize(selection.rows)
    assert summary["by_media_type"] == {"movie": 350, "tv_series": 150}
    assert summary["by_region"]["tamil"] == 20 and summary["by_region"]["korean"] == 35


def test_genre_spread_and_fame_within_cells(selection: c.Selection) -> None:
    cell = [r for r in selection.rows
            if r["role"] == "pilot" and r["bucket"] == "film:english" and r["decade"] == "2010s"]
    assert len({r["genre"] for r in cell}) >= 6
    pool = [it for it in big_pool().values()
            if it["media_type"] == "movie" and it["languages"] == [LANG["english"]]
            and 2010 <= it["year"] < 2020]
    top = max(it["sitelinks"] for it in pool)
    assert max(r["sitelinks"] for r in cell) == top  # the most famous title is always in


def test_reserves_are_ranked_extras(selection: c.Selection) -> None:
    reserves = [r for r in selection.rows if r["role"] == "reserve" and r["bucket"] == "film:tamil"]
    assert len(reserves) == 6  # ceil(20 * 0.3)
    assert [r["bucket_rank"] for r in reserves] == list(range(1, 7))


def test_rows_carry_ids_and_provenance(selection: c.Selection) -> None:
    row = selection.rows[0]
    for key in ("qid", "title", "year", "media_type", "tmdb_id", "imdb_id", "bucket", "role",
                "language", "decade", "genre", "gold_seed"):
        assert key in row
    assert row["source"] == {"kind": "wikidata_sparql", "license": "CC0-1.0",
                             "retrieved_at": "2026-09-30T00:00:00Z"}


def test_selection_is_deterministic() -> None:
    pool = big_pool()
    a = c.select(pool, retrieved_at="t").rows
    b = c.select(dict(reversed(list(pool.items()))), retrieved_at="t").rows
    assert [r["qid"] for r in a] == [r["qid"] for r in b]


def test_seeds_are_forced_in_and_missing_seeds_reported() -> None:
    pool = big_pool()
    obscure = item("movie", "malayalam", 2019, 1, "drama film", country="Q668",
                   title="Kumbalangi Nights")
    pool[obscure["qid"]] = obscure
    seeds = [
        {"title": "Kumbalangi Nights", "year": "2019", "media_type": "movie"},
        {"title": "Nonexistent Film", "year": "1990", "media_type": "movie"},
    ]
    sel = c.select(pool, seeds)
    hit = next(r for r in sel.rows if r["qid"] == obscure["qid"])
    assert hit["role"] == "pilot" and hit["gold_seed"] and hit["bucket"] == "film:malayalam"
    assert hit["gold_seed_title"] == "Kumbalangi Nights"
    assert [s["title"] for s in sel.seeds_missing] == ["Nonexistent Film"]


def test_seed_matching_uses_alt_labels_year_and_type() -> None:
    it = item("movie", "english", 1995, 50, "crime film", title="Seven")
    it["enwiki_title"] = "Seven (1995 film)"
    it["seed_labels"] = ["Se7en"]
    seed = {"title": "Se7en", "year": "1995", "media_type": "movie"}
    assert c.match_seed(seed, [it]) is it
    assert c.match_seed({**seed, "year": "1999"}, [it]) is None
    assert c.match_seed({**seed, "media_type": "tv_series"}, [it]) is None


def test_exclusions() -> None:
    doc = item("movie", "english", 2000, 90, "documentary film")
    concert = item("movie", "english", 2000, 90, "concert film")
    anthology = item("tv_series", "english", 2011, 90, "anthology television series")
    no_article = item("movie", "english", 2000, 90, "drama film")
    no_article["enwiki_title"] = None
    no_tmdb = item("movie", "tamil", 2000, 90, "drama film", country="Q668", tmdb=None)
    ambiguous = item("movie", "english", 2000, 90, "drama film")
    ambiguous.update(tmdb_id=None, tmdb_id_ambiguous=True)
    everything = (doc, concert, anthology, no_article, no_tmdb, ambiguous)
    for it in everything:
        assert c.excluded_reason(it)
    sel = c.select({i["qid"]: i for i in everything})
    assert sel.rows == [] and sum(sel.excluded.values()) == 6
    assert sel.excluded["no_tmdb_id:film:tamil"] == 1
    assert sel.excluded["tmdb_id_ambiguous:film:english"] == 1


@pytest.mark.parametrize(
    "genre,reason",
    [
        ("factual television program", "genre:factual"),  # MythBusters
        ("singing talent show", "genre:talent show"),  # American Idol
        ("educational television", "genre:educational"),  # Barney & Friends
        ("professional wrestling", "genre:professional wrestling"),  # WWE SmackDown
        ("preschool television series", "genre:preschool"),
        ("quiz show", "genre:quiz"),
        ("reality television", "genre:reality"),
        ("game show", "genre:game show"),
    ],
)
def test_non_narrative_series_are_excluded(genre: str, reason: str) -> None:
    assert c.excluded_reason(item("tv_series", "english", 2003, 90, genre)) == reason


def test_listed_non_narrative_titles_are_excluded() -> None:
    """Zoboomafoo's Wikidata genres look like a children's comedy; it is listed by QID."""
    zobo = item("tv_series", "english", 1999, 90, "children's television series")
    zobo["qid"] = "Q3109770"
    assert c.excluded_reason(zobo) == "non_narrative:listed"
    assert c.SELECTOR_VERSION == "1.1.0"


@pytest.mark.parametrize(
    "genre",
    ["time-travel fiction", "music television", "mockumentary", "sports drama",
     "biographical television program", "children's television series", "natural horror film",
     "teen sitcom", "musical"],
)
def test_story_genres_are_not_caught_by_the_new_words(genre: str) -> None:
    assert c.excluded_reason(item("tv_series", "english", 2010, 90, genre)) is None


@pytest.mark.parametrize(
    ("kw", "bucket"),
    [
        ({"media_type": "movie", "lang": "tamil", "country": "Q668"}, "film:tamil"),
        ({"media_type": "movie", "lang": "english", "country": "Q668"}, "film:world"),
        ({"media_type": "movie", "lang": "korean", "country": "Q884"}, "film:korean"),
        ({"media_type": "movie", "lang": "Q150", "country": "Q142"}, "film:world"),
        ({"media_type": "tv_series", "lang": "hindi", "country": "Q668"}, "tv:indian"),
        ({"media_type": "tv_series", "lang": "japanese", "country": "Q17",
          "classes": ["Q63952888"]}, "tv:anime"),
        ({"media_type": "tv_series", "lang": "japanese", "country": "Q17"}, "tv:world"),
        ({"media_type": "tv_series", "lang": "english", "country": "Q145"}, "tv:english"),
    ],
)
def test_classify(kw: dict[str, Any], bucket: str) -> None:
    it = item(kw["media_type"], kw["lang"], 2010, 10, "drama", country=kw["country"],
              classes=kw.get("classes"))
    assert c.classify(it) == bucket


def test_coarse_genre_priority() -> None:
    assert c.coarse_genre(["comedy film", "animated film"]) == "animation"
    assert c.coarse_genre(["horror comedy"]) == "horror"
    assert c.coarse_genre(["superhero film"]) == "action"
    assert c.coarse_genre(["romantic comedy"]) == "comedy"
    assert c.coarse_genre([]) == "other"


def test_limit_interleaves_buckets(selection: c.Selection) -> None:
    rows = c.interleave_limit(selection.rows, 13)
    assert len(rows) == 13 and len({r["bucket"] for r in rows}) == 13


def test_sparql_rendering() -> None:
    queries = c.pool_queries()
    ids = [q.query_id for q in queries]
    assert len(ids) == len(set(ids))
    q = next(q for q in queries if q.query_id == "film:english:1990s")
    text = q.sparql()
    assert "wdt:P364 wd:Q1860" in text
    assert "HAVING (MIN(?year_) >= 1990 && MIN(?year_) < 2000)" in text
    assert "schema:isPartOf <https://en.wikipedia.org/>" in text and "LIMIT 105" in text
    tv = PoolQuery("x", "tv_series", "", 2000, 2010, 5, 10).sparql()
    assert "(wdt:P580|wdt:P577)" in tv and "wd:Q5398426" in tv
    detail = detail_sparql(["Q1", "Q2"])
    assert "VALUES ?item { wd:Q1 wd:Q2 }" in detail
    for prop in ("P4947", "P4983", "P345", "P364", "P495", "P136", "P582"):
        assert f"wdt:{prop}" in detail
    seeds = seed_sparql(['He said "hi"', "Amélie"])
    assert '"He said \\"hi\\""@en' in seeds and "skos:altLabel" in seeds


def test_parse_details_ids_and_series_status() -> None:
    data = FakeWikimedia().data["sparql"]["detail"]
    items = parse_details(data)
    film, tv = items["Q9000001"], items["Q9000003"]
    assert film["media_type"] == "movie" and film["tmdb_id"] == 90001
    assert film["imdb_id"] == "tt9000001" and film["series_status"] is None
    assert tv["media_type"] == "tv_series" and tv["tmdb_id"] == 90003
    assert tv["series_status"] == "ended" and tv["end_year"] == 2018
    assert tv["series_status_basis"] == "P582"
    # no end time in Wikidata is not evidence the series is ongoing (schema 1.1.0)
    assert items["Q9000004"]["series_status"] == "unknown"
    assert items["Q9000004"]["series_status_basis"] == "none"
    assert film["series_status_basis"] is None


def test_series_status_needs_positive_evidence() -> None:
    data = FakeWikimedia().data["sparql"]["detail"]
    tv = next(b for b in data["results"]["bindings"] if b["item"]["value"].endswith("Q9000004"))
    no_end = {**tv, "no_end": {"type": "literal", "value": "1"}}
    both = {**no_end, "end_year": {"type": "literal", "value": "2019"}}
    for binding, want in ((no_end, ("ongoing", "P582_novalue")), (both, ("unknown", "conflict"))):
        item = parse_details({"results": {"bindings": [binding]}})["Q9000004"]
        assert (item["series_status"], item["series_status_basis"]) == want
    assert series_status(False, 2019, True) == (None, None)
    assert "wdno:P582" in detail_sparql(["Q1"])


@pytest.mark.parametrize(
    "candidate,want",
    [
        ({"media_type": "tv_series", "series_status": "ongoing"}, "unknown"),  # pre-1.1.0 file
        ({"media_type": "tv_series", "series_status": "ongoing", "series_status_basis": None},
         "unknown"),
        ({"media_type": "tv_series", "series_status": "ended"}, "ended"),
        ({"media_type": "tv_series", "series_status": "ongoing",
          "series_status_basis": "P582_novalue"}, "ongoing"),
        ({"media_type": "tv_series", "series_status": "unknown", "series_status_basis": "none"},
         "unknown"),
        ({"media_type": "movie", "series_status": None}, None),
    ],
)
def test_effective_series_status_reads_old_ongoing_as_unknown(candidate, want) -> None:
    assert effective_series_status(candidate) == want


def test_ambiguous_tmdb_id_is_dropped() -> None:
    data = FakeWikimedia().data["sparql"]["detail"]
    binding = dict(data["results"]["bindings"][0])
    binding["tmdb_movie"] = {"type": "literal", "value": "90001|123"}
    parsed = parse_details({"results": {"bindings": [binding]}})["Q9000001"]
    assert parsed["tmdb_id"] is None and parsed["tmdb_id_ambiguous"]


def test_gather_runs_pool_seed_and_detail_queries() -> None:
    fake = FakeWikimedia()
    wd = Wikidata(HttpClient(fake, None, sleep=lambda s: None, min_interval={}))
    items = c.gather(wd, [{"title": "Lantern Keeper", "year": "1994", "media_type": "movie"}])
    assert set(items) == {"Q9000001", "Q9000002", "Q9000003", "Q9000004", "Q9000006"}
    assert items["Q9000001"]["seed_labels"] == ["Lantern Keeper"]
    assert fake.hosts == {"query.wikidata.org"}
    assert all(r.method == "POST" for r in fake.requests)


def test_language_labels_come_from_wikidata() -> None:
    """Reports show other languages by their Wikidata label, not a bare QID."""
    data = FakeWikimedia().data["sparql"]["detail"]
    binding = {
        **data["results"]["bindings"][0],
        "languages": {"type": "literal", "value": "Q188|Q7976"},
        "language_labels": {"type": "literal",
                            "value": "Q188=German|Q7976=American English|bad"},
    }
    item = parse_details({"results": {"bindings": [binding]}})["Q9000001"]
    assert item["language_labels"] == {"Q188": "German", "Q7976": "American English"}
    assert "rdfs:label ?langLabel_" in detail_sparql(["Q1"])
    assert c.language_display({"language": "Q188", "language_labels": {"Q188": "German"}}) == (
        "German (Q188)"
    )
    assert c.language_display({"language": "english", "language_labels": {}}) == "english"
    assert c.language_display({"language": "Q7979"}) == "Q7979"  # older files: no labels
    assert c.language_display({"language": None}) == "unknown"
