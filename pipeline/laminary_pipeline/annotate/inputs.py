"""Plot-summary inputs and the fail-closed gate in front of every model call.

Interface with ingestion (files under ``pipeline/data/plots/``, one JSON object per title, as
``*.json`` files or one object per line in ``*.jsonl``). Two shapes are accepted:

1. Ingest's plot file (``laminary_pipeline.ingest.wikipedia``, ``data/plots/<QID>.json``)::

       {"qid": "Q83495", "status": "ok",
        "candidate": {"title", "year", "media_type", "tmdb_id", "series_status", ...},
        "source": {<the schema's source object>}, "text": "<the exact plot text>", ...}

   Files with ``status`` other than ``ok`` are reported as unusable, with their skip reason.
   A series whose main article was too thin may instead carry ``"via": "season_articles"`` and
   ``"sources": [{"source": {...}, "text": "...", ...}, ...]``, one entry per season article in
   season order (DECISIONS 2026-10-01); each becomes its own source and summary block. From
   fetcher 1.5.0 such a file may also carry ``"via_detail": "episode_table"`` (DECISIONS
   2026-10-02): the sources are per-episode summaries from episode tables, one per season,
   and ``coverage.partial_season`` may name the last season when only some of its episodes
   fit under the cap. Fetcher 1.5.5 (a run rule numbering by year, CID): each source carries
   ``year`` instead of ``season`` and ``coverage`` is ``{"unit": "year", "years",
   "first_year", "last_year", "partial_year"?}`` (``parse_year_coverage``).

2. Generic: ``{"title": {<the schema's title object>}, "sources": [{<source>, "text": ...}]}``.

``content_sha256`` is the SHA-256 of ``text`` as UTF-8 and ``word_count`` is
``laminary_pipeline.ingest.text.word_count(text)``, the single word counter shared with ingest.
The title key is ``<media_type>:<tmdb_id>`` (``movie:603``); the QID or file stem also works on
the command line. A title without an integer ``tmdb_id`` is unusable (LLM records require one).

The gate (docs/NARRATIVE_SCHEMA.md section 1, Phase 1 requirements) runs before any request
is built and again on the built request (``prompt.verify_request``):

- every source passes ``annotation.require_wikipedia_sources`` (one bad source rejects the
  title; TMDB text never gets through);
- SHA-256 of each exact text equals the source's ``content_sha256``;
- each text's word count equals its ``word_count``, and the total is at least 150;
- at most the schema's ``provenance.sources.maxItems`` sources, and season-article input
  (``via: "season_articles"``, several sources, or one with a season) at most the 6,000-word
  ceiling, or the ~3,000-word cap for a lone episode-list page or for episode-table input
  (``check_source_limits``), so a record that could not be stored, or that ingest could not
  have produced, is never paid for.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from laminary_pipeline.annotation import format_checker, load_schema, require_wikipedia_sources
from laminary_pipeline.ingest.episodes import MIN_YEAR, UNIT_YEAR, VIA_DETAIL_EPISODE_TABLE
from laminary_pipeline.ingest.seasons import (
    COVERAGE_BASES,
    LEAD_BLOCK_CEILING,
    MAX_SEASON_NUMBER,
    SEASON_WORD_CAP,
)
from laminary_pipeline.ingest.text import word_count
from laminary_pipeline.ingest.wikidata import effective_series_status
from laminary_pipeline.ingest.wikipedia import VIA_SEASON_ARTICLES

MIN_SUMMARY_WORDS = 150  # DECISIONS.md 2026-09-26


class GateError(ValueError):
    """A title that must not be sent to the model. The message says why."""


class InputFormatError(ValueError):
    """A plot file that does not match the ingestion interface."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def count_words(text: str) -> int:
    """Ingest's word counter, so the gate and the stored word_count always agree."""
    return word_count(text)


def _def_validator(name: str) -> Draft202012Validator:
    schema = load_schema()
    return Draft202012Validator(
        {"$defs": schema["$defs"], "$ref": f"#/$defs/{name}"}, format_checker=format_checker()
    )


@dataclass(frozen=True)
class PlotSource:
    meta: dict[str, Any]  # exactly the schema's source fields; stored in provenance
    text: str


@dataclass(frozen=True)
class PlotInput:
    title: dict[str, Any]
    sources: tuple[PlotSource, ...]
    origin: str  # file (and line) it came from
    via: str | None = None  # "season_articles" when ingest built it from season articles
    # Season-article input (DECISIONS 2026-10-02): the series' season total from the plot
    # file's ``coverage`` (validated in parse_plot) and where it came from
    season_total: int | None = None
    season_total_basis: str | None = None
    # ingest's fetcher_version for an ingest plot file ("0" when the file has none); None for
    # the generic shape. annotate-1.2.0 refuses files from before fetcher 1.4.0.
    fetcher_version: str | None = None
    # "episode_table" for per-episode summaries from episode tables (fetcher 1.5.0), else None
    via_detail: str | None = None
    # episode-table input: the last season, when only some of its episodes fit under the cap
    # (validated in parse_plot: a plain int equal to the last source's season)
    partial_season: int | None = None
    # episode-table input numbered by year (fetcher 1.5.5, CID): the span of years the list
    # pages name, (first, last), and the last year when it is only partly included
    year_span: tuple[int, int] | None = None
    partial_year: int | None = None

    @property
    def key(self) -> str:
        return title_key(self.title)


def title_key(title: dict[str, Any]) -> str:
    return f"{title['media_type']}:{title['tmdb_id']}"


class NotAnnotatable(ValueError):
    """A well-formed plot file that ingestion itself marked as skipped, or lacks a tmdb_id."""


def _from_ingest_shape(obj: dict[str, Any], origin: str) -> dict[str, Any]:
    if obj.get("status") != "ok":
        raise NotAnnotatable(
            f"ingest status {obj.get('status')!r}: {obj.get('skip_reason')} "
            f"{obj.get('skip_detail') or ''}".strip()
        )
    cand = obj.get("candidate") or {}
    title: dict[str, Any] = {
        "media_type": cand.get("media_type"),
        "name": cand.get("title"),
        "release_year": cand.get("year"),
        "tmdb_id": cand.get("tmdb_id"),
        "wikidata_id": obj.get("qid"),
    }
    if cand.get("media_type") == "tv_series":
        title["series_status"] = effective_series_status(cand)
    out = {"title": title, "sources": ingest_sources(obj), "via": ingest_via(obj),
           "fetcher_version": str(obj.get("fetcher_version") or "0")}
    if "via_detail" in obj:
        out["via_detail"] = obj["via_detail"]
    if "coverage" in obj:
        out["coverage"] = obj["coverage"]
    return out


def parse_coverage(
    coverage: Any, sources: list[PlotSource], origin: str
) -> tuple[int | None, str | None]:
    """(total seasons, basis) from a plot file's ``coverage``, or (None, None) without one.
    Raises InputFormatError unless the total is a plain int from 1 to 100 with a known basis,
    and the listed seasons are exactly the sources' season numbers."""
    if coverage is None:
        return None, None
    if not isinstance(coverage, dict):
        raise InputFormatError(f"{origin}: coverage must be an object")
    total = coverage.get("total_seasons")
    basis = coverage.get("total_seasons_basis")
    if total is not None and (type(total) is not int or not 1 <= total <= MAX_SEASON_NUMBER):
        raise InputFormatError(f"{origin}: coverage.total_seasons {total!r} is not a whole "
                               f"number from 1 to {MAX_SEASON_NUMBER}")
    if (total is None) != (basis is None) or (basis is not None and basis not in
                                               COVERAGE_BASES):
        raise InputFormatError(f"{origin}: coverage.total_seasons_basis {basis!r} is not one of "
                               f"{list(COVERAGE_BASES)} (or the total is missing)")
    listed = coverage.get("seasons")
    if listed is not None and listed != [s.meta.get("season") for s in sources]:
        raise InputFormatError(f"{origin}: coverage.seasons {listed!r} doesn't match the "
                               "sources' season numbers")
    return total, basis


MIN_FETCHER_FOR_EPISODE_TABLES = (1, 5, 0)
MAX_YEARS_AHEAD = 2  # a coverage year may be at most this far past the current year


def latest_coverage_year() -> int:
    return datetime.now(UTC).year + MAX_YEARS_AHEAD


def parse_year_coverage(
    coverage: Any, sources: list[PlotSource], via_detail: str | None, origin: str
) -> tuple[tuple[int, int] | None, int | None]:
    """Year coverage (fetcher 1.5.5, CID): ``((first_year, last_year), partial_year)``, or
    ``(None, None)`` when neither the coverage nor any source is by year. Raises
    InputFormatError unless it is episode-table input whose every source has a ``year`` and
    no ``season``, ``coverage.years`` equals the sources' years in increasing order, the span
    is plain ints with MIN_YEAR <= first_year <= the first year, the last year <= last_year <=
    the current year + 2, and ``partial_year`` is absent or the last source's year."""
    unit = coverage.get("unit") if isinstance(coverage, dict) else None
    by_year = [("year" in s.meta) for s in sources]
    if unit is None and not any(by_year):
        return None, None
    if unit != UNIT_YEAR or not isinstance(coverage, dict):
        raise InputFormatError(f"{origin}: sources with a year need coverage.unit 'year' "
                               f"(got {unit!r})")
    if via_detail != VIA_DETAIL_EPISODE_TABLE:
        raise InputFormatError(f"{origin}: year coverage without episode-table input")
    if not all(by_year) or any("season" in s.meta for s in sources):
        raise InputFormatError(f"{origin}: with year coverage every source needs a year and "
                               "none a season")
    years = [s.meta["year"] for s in sources]
    if coverage.get("years") != years:
        raise InputFormatError(f"{origin}: coverage.years {coverage.get('years')!r} doesn't "
                               f"match the sources' years {years}")
    if any(type(y) is not int for y in years) or any(
            b <= a for a, b in zip(years, years[1:], strict=False)):
        raise InputFormatError(f"{origin}: years {years} are not whole years in increasing "
                               "order")
    first, last = coverage.get("first_year"), coverage.get("last_year")
    latest = latest_coverage_year()
    if not (type(first) is int and type(last) is int
            and MIN_YEAR <= first <= years[0] and years[-1] <= last <= latest):
        raise InputFormatError(f"{origin}: coverage years {first!r}–{last!r} don't hold "
                               f"{years[0]}–{years[-1]} within {MIN_YEAR}–{latest}")
    partial = coverage.get("partial_year")
    if partial is not None and (type(partial) is not int or partial != years[-1]):
        raise InputFormatError(f"{origin}: coverage.partial_year {partial!r} is not the last "
                               "source's year")
    return (first, last), partial


def parse_via_detail(obj: dict[str, Any], via: Any, origin: str) -> str | None:
    """The plot file's ``via_detail``: None, or ``"episode_table"`` on season-article input
    from fetcher 1.5.0 or later (an ingest file) or the generic shape. Anything else raises
    InputFormatError."""
    detail = obj.get("via_detail")
    if detail is None:
        return None
    if detail != VIA_DETAIL_EPISODE_TABLE:
        raise InputFormatError(f"{origin}: via_detail {detail!r} is not "
                               f"{VIA_DETAIL_EPISODE_TABLE!r}")
    if via != VIA_SEASON_ARTICLES:
        raise InputFormatError(f"{origin}: via_detail {detail!r} without via "
                               f"{VIA_SEASON_ARTICLES!r}")
    fetcher = obj.get("fetcher_version")
    if isinstance(fetcher, str):
        parts = fetcher.split(".")
        got = tuple(int(p) for p in parts) if all(p.isdigit() for p in parts) else None
        if got is None or got < MIN_FETCHER_FOR_EPISODE_TABLES:
            raise InputFormatError(f"{origin}: episode-table input from fetcher {fetcher!r}, "
                                   "which can't produce it")
    return detail


def parse_partial_season(
    coverage: Any, sources: list[PlotSource], via_detail: str | None, origin: str
) -> int | None:
    """``coverage.partial_season``: None, or (episode-table input only) a plain int equal to
    the last source's season number. Anything else raises InputFormatError."""
    if not isinstance(coverage, dict) or coverage.get("partial_season") is None:
        return None
    value = coverage["partial_season"]
    if via_detail != VIA_DETAIL_EPISODE_TABLE:
        raise InputFormatError(f"{origin}: coverage.partial_season without episode-table "
                               "input")
    if type(value) is not int or not sources or value != sources[-1].meta.get("season"):
        raise InputFormatError(f"{origin}: coverage.partial_season {value!r} is not the last "
                               "source's season number")
    return value


def ingest_via(obj: dict[str, Any]) -> str | None:
    """``"season_articles"`` for an ingest plot file built from season articles (the same
    test ``ingest_sources`` uses), else its ``via`` (None for a main article)."""
    if obj.get("via") == VIA_SEASON_ARTICLES or "sources" in obj:
        return VIA_SEASON_ARTICLES
    via = obj.get("via")
    return via if isinstance(via, str) else None


def ingest_sources(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """An ok ingest plot file's sources, in order, as ``{<source fields>, "text": ...}``: the
    season articles of a ``season_articles`` file, else its single source."""
    if ingest_via(obj) == VIA_SEASON_ARTICLES:
        parts = obj.get("sources")
        if not isinstance(parts, list) or not parts:
            raise InputFormatError("season-article plot file without sources")
        return [{**(p.get("source") or {}), "text": p.get("text")} for p in parts]
    return [{**(obj.get("source") or {}), "text": obj.get("text")}]


def parse_plot(obj: Any, origin: str) -> PlotInput:
    """Check one plot object against the interface and the schema's title/source shapes."""
    if not isinstance(obj, dict):
        raise InputFormatError(f"{origin}: expected an object")
    if "candidate" in obj or "qid" in obj:
        obj = _from_ingest_shape(obj, origin)
    title, sources = obj.get("title"), obj.get("sources")
    if isinstance(title, dict) and not isinstance(title.get("tmdb_id"), int):
        raise NotAnnotatable("no integer tmdb_id (LLM records require one)")
    if isinstance(title, dict) and title.get("release_year") is None:
        raise NotAnnotatable("no release year (records require release_year)")
    errors = [e.message for e in _def_validator("title").iter_errors(title)]
    if errors:
        raise InputFormatError(f"{origin}: title: {'; '.join(errors)}")
    if not isinstance(sources, list) or not sources:
        raise InputFormatError(f"{origin}: sources must be a non-empty list")
    source_fields = set(load_schema()["$defs"]["source"]["properties"])
    parsed = []
    for i, src in enumerate(sources):
        if not isinstance(src, dict) or not isinstance(src.get("text"), str):
            raise InputFormatError(f"{origin}: source {i} needs a string 'text'")
        meta = {k: v for k, v in src.items() if k != "text"}
        extra = set(meta) - source_fields
        if extra:
            raise InputFormatError(f"{origin}: source {i} has unknown fields {sorted(extra)}")
        errors = [e.message for e in _def_validator("source").iter_errors(meta)]
        if errors:
            raise InputFormatError(f"{origin}: source {i}: {'; '.join(errors)}")
        parsed.append(PlotSource(meta=meta, text=src["text"]))
    via = obj.get("via")
    total, basis = parse_coverage(obj.get("coverage"), parsed, origin)
    fetcher = obj.get("fetcher_version")
    via_detail = parse_via_detail(obj, via, origin)
    partial = parse_partial_season(obj.get("coverage"), parsed, via_detail, origin)
    span, partial_year = parse_year_coverage(obj.get("coverage"), parsed, via_detail, origin)
    return PlotInput(title=title, sources=tuple(parsed), origin=origin,
                     via=via if isinstance(via, str) else None,
                     season_total=total, season_total_basis=basis,
                     fetcher_version=fetcher if isinstance(fetcher, str) else None,
                     via_detail=via_detail, partial_season=partial,
                     year_span=span, partial_year=partial_year)


def iter_plot_files(plots_dir: Path) -> Iterator[tuple[Any, str]]:
    for path in sorted(plots_dir.rglob("*")):
        if path.suffix == ".json":
            yield json.loads(path.read_text(encoding="utf-8")), str(path)
        elif path.suffix == ".jsonl":
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if line.strip():
                    yield json.loads(line), f"{path}:{n}"


@dataclass
class LoadResult:
    plots: list[PlotInput]
    unusable: list[tuple[str, str]]  # (origin, reason): never sent, reported in skipped.jsonl


def load_plots(plots_dir: Path) -> LoadResult:
    """All usable plot inputs sorted by title key, plus every file that can't be used and why.
    Malformed files are reported, not fatal; a duplicate title key is fatal (a data bug)."""
    plots: dict[str, PlotInput] = {}
    unusable: list[tuple[str, str]] = []
    for obj, origin in iter_plot_files(plots_dir):
        try:
            plot = parse_plot(obj, origin)
        except (InputFormatError, NotAnnotatable) as e:
            unusable.append((origin, str(e)))
            continue
        if plot.key in plots:
            raise InputFormatError(
                f"duplicate title {plot.key}: {plots[plot.key].origin}, {origin}"
            )
        plots[plot.key] = plot
    return LoadResult([plots[k] for k in sorted(plots)], unusable)


def find_plot(plots: list[PlotInput], wanted: str) -> PlotInput:
    """By title key (movie:603), Wikidata QID or file stem."""
    for plot in plots:
        stem = Path(plot.origin.split(".json")[0]).name  # "d/Q1.json", "d/f.jsonl:3"
        if wanted in (plot.key, plot.title.get("wikidata_id"), stem):
            return plot
    raise KeyError(f"no plot input for title {wanted!r}")


@dataclass(frozen=True)
class GatedInput:
    """A plot input that passed the gate. Only ``gate()`` creates one."""

    plot: PlotInput

    @property
    def source_metas(self) -> list[dict[str, Any]]:
        return [dict(s.meta) for s in self.plot.sources]

    @property
    def input_word_count(self) -> int:
        return sum(s.meta["word_count"] for s in self.plot.sources)

    @property
    def source_hashes(self) -> tuple[str, ...]:
        return tuple(sorted(s.meta["content_sha256"] for s in self.plot.sources))


def check_text(text: str, meta: dict[str, Any], label: str) -> None:
    """The hash and word-count checks on the exact text that will be sent."""
    digest = sha256_text(text)
    if digest != meta.get("content_sha256"):
        raise GateError(
            f"{label}: sha256 of text {digest} != content_sha256 {meta.get('content_sha256')}"
        )
    words = count_words(text)
    if words != meta.get("word_count"):
        raise GateError(
            f"{label}: text has {words} words but word_count is {meta.get('word_count')}"
        )


def gate(plot: PlotInput) -> GatedInput:
    """Fail closed: raise GateError unless the title may be sent to the model."""
    try:
        require_wikipedia_sources([s.meta for s in plot.sources])
    except ValueError as e:
        raise GateError(f"{plot.key}: {e}") from e
    for i, src in enumerate(plot.sources):
        check_text(src.text, src.meta, f"{plot.key} source {i}")
    total = sum(count_words(s.text) for s in plot.sources)
    if total < MIN_SUMMARY_WORDS:
        raise GateError(f"{plot.key}: {total} words of summary, minimum is {MIN_SUMMARY_WORDS}")
    check_source_limits(plot, total)
    return GatedInput(plot)


def max_sources() -> int:
    """The schema's limit on provenance.sources: a record with more could not be stored."""
    return int(load_schema()["$defs"]["provenance"]["properties"]["sources"]["maxItems"])


def check_source_limits(plot: PlotInput, total: int) -> None:
    """Refuse inputs that ingest's season-article rules (DECISIONS 2026-10-01) can't produce,
    before any paid call: more sources than a record may hold; any season-article input over
    its word limit. Season-article input is ``via: "season_articles"``, several sources, or
    one with a season. Its limit is the 6,000-word ceiling (a lead block of stub seasons plus
    the first full season may be several sources up to it, so the ~3,000-word cap is a joining
    rule, not an input limit), except a lone episode-list page (no season) from season
    articles: ingest joins that only under the cap, so the tighter cap applies. Episode-table
    input (``via_detail: "episode_table"``, fetcher 1.5.0) is joined at episode boundaries
    under the cap and never uses a lead block, so the cap applies to it too, and every source
    must carry a season. A single main article has no upper word limit (unchanged)."""
    n = len(plot.sources)
    if n > max_sources():
        raise GateError(f"{plot.key}: {n} sources, a record holds at most {max_sources()}")
    has_season = any("season" in s.meta for s in plot.sources)
    via_seasons = plot.via == VIA_SEASON_ARTICLES
    if plot.via_detail == VIA_DETAIL_EPISODE_TABLE:
        unit = "year" if plot.year_span is not None else "season"
        if not via_seasons or not all(unit in s.meta for s in plot.sources):
            raise GateError(f"{plot.key}: episode-table input must be season-article input "
                            f"with a {unit} on every source")
        limit, what = SEASON_WORD_CAP, "episode-table cap"
    elif not (via_seasons or n > 1 or has_season):
        return  # a single main article
    elif via_seasons and n == 1 and not has_season:
        limit, what = SEASON_WORD_CAP, "episode-list page"
    else:
        limit, what = LEAD_BLOCK_CEILING, "ceiling"
    if total > limit:
        raise GateError(
            f"{plot.key}: season-article input of {n} source(s) and {total} words, over the "
            f"{limit}-word {what}"
        )


def read_titles_file(path: Path) -> list[str]:
    """Title identifiers from a selection file, in file order, without duplicates.

    Accepts ingest's ``pilot_effective.jsonl``, the gold ``gold_selection.jsonl`` (one JSON
    object per line; its ``qid``, else ``title_key``/``key``, is used), or a plain text list
    with one QID or title key (``movie:603``) per line (blank lines and ``#`` comments
    ignored)."""
    wanted: list[str] = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("{"):
            obj = json.loads(line)
            value = obj.get("qid") or obj.get("title_key") or obj.get("key")
            if not isinstance(value, str) or not value:
                raise InputFormatError(f"{path}:{n}: no qid, title_key or key")
        else:
            value = line
        if value not in wanted:
            wanted.append(value)
    if not wanted:
        raise InputFormatError(f"{path}: no titles listed")
    return wanted


def select_plots(
    plots: list[PlotInput], wanted: list[str]
) -> tuple[list[PlotInput], list[str]]:
    """The plots named in ``wanted`` (by title key or QID), in ``wanted`` order, and the
    names with no usable plot input."""
    by_name: dict[str, PlotInput] = {}
    for plot in plots:
        by_name[plot.key] = plot
        qid = plot.title.get("wikidata_id")
        if qid:
            by_name[qid] = plot
    selected: list[PlotInput] = []
    missing: list[str] = []
    for name in wanted:
        plot = by_name.get(name)
        if plot is None:
            missing.append(name)
        elif plot not in selected:
            selected.append(plot)
    return selected, missing
