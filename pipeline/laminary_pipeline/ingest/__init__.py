"""Title ingestion for the Phase 1 pilot (docs/PLAN.md section 5).

- ``candidates``: Wikidata SPARQL (CC0) -> the 500-title pilot list with cross-IDs.
- ``wikipedia``: English Wikipedia plot sections (CC BY-SA) -> ``data/plots/<QID>.json``.

No TMDB API calls and no TMDB text anywhere (docs/NARRATIVE_SCHEMA.md section 1). TMDB ids
come from Wikidata (P4947 movie, P4983 TV) as plain identifiers. The HTTP client only talks to
the hosts in ``http.ALLOWED_HOSTS``.
"""
