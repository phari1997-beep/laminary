"""Fetch plot sections for the pilot candidates, resumably, and report pass rates.

One file per title: ``data/plots/<QID>.json`` with ``status`` "ok" (text + a schema-shaped
``source``) or "skipped" (with ``skip_reason``). A title with a file is not fetched again unless
``--refresh`` is given; ``fetch_error`` files (transient network problems) are always retried,
and so are series skipped as too thin before the season-article fallback (fetcher 1.1.0) or the
episode-table fallback (fetcher 1.5.0) existed, or, for a series with a run rule
(``runs.SERIES_RUN_RULES``, Doctor Who), before the rule existed (fetcher 1.5.1), or with an
episode-list page skipped as unverified before the main-article link fallback (fetcher 1.5.2),
and priority series that passed on a main article under 500 words before the richer-text rule
(fetcher 1.5.2), and files shaped by the link fallback before its hardening (fetcher 1.5.3).

**Priority series** (DECISIONS 2026-10-02): ten big shows Hari named must not drop out of the
pilot (``PRIORITY_SERIES``). The report lists any that ended skipped, with the reason, and
backfill never replaces one: its pilot slot is held (left empty in the effective pilot) and a
warning for Hari is printed instead.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from laminary_pipeline.ingest.candidates import BUCKETS, language_display
from laminary_pipeline.ingest.episodes import VIA_DETAIL_EPISODE_TABLE
from laminary_pipeline.ingest.paths import DataPaths, read_json, write_json_atomic
from laminary_pipeline.ingest.priority import PRIORITY_SERIES
from laminary_pipeline.ingest.runs import SERIES_RUN_RULES
from laminary_pipeline.ingest.wikipedia import (
    RICHER_TEXT_WORDS,
    SKIP_FETCH_ERROR,
    SKIP_NO_SECTION,
    SKIP_SEASON_TOO_LONG,
    SKIP_TOO_SHORT,
    VIA_SEASON_ARTICLES,
    PlotFetcher,
)

EPISODE_TABLE_FETCHER = (1, 5, 0)
RUN_RULE_FETCHER = (1, 5, 1)
LIST_LINK_FETCHER = (1, 5, 2)
RICHER_TEXT_FETCHER = (1, 5, 2)
LINK_HARDENING_FETCHER = (1, 5, 6)  # 1.5.3 back-link; 1.5.6 lead-only back-link
YEAR_HEADING_FIX_FETCHER = (1, 5, 6)  # 1.5.5's table_heading read years for every series


def existing_status(paths: DataPaths, qid: str) -> dict[str, Any] | None:
    path = paths.plot_file(qid)
    return read_json(path) if path.exists() else None


def needs_fetch(paths: DataPaths, qid: str, refresh: bool) -> bool:
    if refresh:
        return True
    current = existing_status(paths, qid)
    return current is None or current.get("skip_reason") == SKIP_FETCH_ERROR or (
        _thin_series_before_seasons(current) or _thin_series_before_episode_tables(current)
        or _run_rule_series_before_rules(current)
        or _unverified_list_page_before_link_fallback(current)
        or _thin_priority_series_before_richer_text(current)
        or _link_fallback_before_hardening(current)
        or _episode_tables_from_1_5_5(current)
    )


def _version(value: Any) -> tuple[int, ...]:
    parts = str(value or "0").split(".")
    return tuple(int(p) for p in parts) if all(p.isdigit() for p in parts) else (0,)


def _thin_series_before_episode_tables(rec: dict[str, Any]) -> bool:
    """A series skipped as thin (or with a season article over the ceiling) after trying season
    articles, by a fetcher older than the episode-table fallback (1.5.0): fetched again so the
    fallback can run. Not when the season-article lookup was disabled for that run."""
    return (_thin_series_tried_seasons(rec)
            and _version(rec.get("fetcher_version")) < EPISODE_TABLE_FETCHER)


def _run_rule_series_before_rules(rec: dict[str, Any]) -> bool:
    """A series with a run rule (``runs.py``) whose file predates its rule's current form
    (``SeriesRunRule.since``: Doctor Who 1.5.4, the ordinal offset): fetched again so the rule
    can apply, whether the file is skipped or ok (an ok file may come from a thin main article
    that the rule's season text could beat, priority series only, fetcher 1.5.2)."""
    rule = SERIES_RUN_RULES.get(str(rec.get("qid")))
    if rule is None or (rec.get("candidate") or {}).get("media_type") != "tv_series":
        return False
    since = max(RUN_RULE_FETCHER, rule.since)
    return _version(rec.get("fetcher_version")) < since and rec.get("skip_reason") != (
        SKIP_FETCH_ERROR)  # fetch errors are retried anyway


def _unverified_list_page_before_link_fallback(rec: dict[str, Any]) -> bool:
    """A series skipped after trying season articles, with an episode-list page skipped as
    unverified, by a fetcher older than the main-article link fallback (1.5.2): fetched again
    so the fallback can verify the page (Midsomer Murders, CID)."""
    skipped = (rec.get("season_articles") or {}).get("skipped") or []
    return (
        _thin_series_tried_seasons(rec)
        and _version(rec.get("fetcher_version")) < LIST_LINK_FETCHER
        and any(str(s.get("title", "")).startswith("List of")
                and str(s.get("reason", "")).startswith("unverified")
                for s in skipped if isinstance(s, dict))
    )


def _thin_priority_series_before_richer_text(rec: dict[str, Any]) -> bool:
    """A priority series that passed on its main article with under RICHER_TEXT_WORDS words, by
    a fetcher older than the richer-text rule (1.5.2): fetched again so the season-article /
    episode-table text can be tried (Sherlock, Doctor Who, Star Trek: TNG, M*A*S*H)."""
    words = rec.get("word_count")
    return (
        rec.get("qid") in PRIORITY_SERIES
        and rec.get("status") == "ok"
        and (rec.get("candidate") or {}).get("media_type") == "tv_series"
        and rec.get("via") is None
        and isinstance(words, int) and words < RICHER_TEXT_WORDS
        and _version(rec.get("fetcher_version")) < RICHER_TEXT_FETCHER
    )


def _episode_tables_from_1_5_5(rec: dict[str, Any]) -> bool:
    """A file from fetcher 1.5.5 that tried episode tables: that version's ``table_heading``
    let a year subheading decide a table on every series ("Season 1" > "2005" was skipped,
    "Specials" > "2010" was not). Fetched again with the fix (1.5.6, QA)."""
    return ("episode_tables" in rec
            and _version(rec.get("fetcher_version")) == (1, 5, 5)
            and rec.get("qid") not in SERIES_RUN_RULES)  # rule titles: their own ``since``


def _link_fallback_before_hardening(rec: dict[str, Any]) -> bool:
    """A file shaped by the main-article link fallback before its hardening (1.5.3: the list
    page must link back; only a TV series rejects; 1.5.6: only the lead's links count as the
    back-link): one built from a link-verified list page, or one with a list page rejected
    for a statement to another item. Fetched again so the current checks apply."""
    if _version(rec.get("fetcher_version")) >= LINK_HARDENING_FETCHER:
        return False
    evidence = [*((rec.get("season_articles") or {}).get("evidence") or []),
                *((rec.get("episode_tables") or {}).get("evidence") or [])]
    skipped = (rec.get("season_articles") or {}).get("skipped") or []
    return (
        any(isinstance(e, dict) and e.get("basis") == "main_article_link" for e in evidence)
        or any(isinstance(s, dict) and "in another" in str(s.get("reason", ""))
               for s in skipped)
    )


def _thin_series_tried_seasons(rec: dict[str, Any]) -> bool:
    return (
        rec.get("skip_reason") in (SKIP_TOO_SHORT, SKIP_NO_SECTION, SKIP_SEASON_TOO_LONG)
        and (rec.get("candidate") or {}).get("media_type") == "tv_series"
        and "season_articles" in rec
        and (rec.get("season_articles") or {}).get("status") != "disabled"
    )


def _thin_series_before_seasons(rec: dict[str, Any]) -> bool:
    """A series skipped as thin by a fetcher older than the season-article fallback (1.1.0):
    fetched again so the fallback can run. Any record with a ``season_articles`` entry
    (attempted, or disabled for that run) is not re-fetched without --refresh."""
    return (
        rec.get("skip_reason") in (SKIP_TOO_SHORT, SKIP_NO_SECTION)
        and (rec.get("candidate") or {}).get("media_type") == "tv_series"
        and "season_articles" not in rec
    )


@dataclass
class PlotRun:
    fetched: int = 0
    skipped_existing: int = 0
    backfilled: int = 0
    planned: int = 0


def run_plots(
    paths: DataPaths,
    candidates: Sequence[dict[str, Any]],
    fetcher: PlotFetcher | None,
    *,
    limit: int | None = None,
    refresh: bool = False,
    backfill: bool = False,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> PlotRun:
    run = PlotRun()
    pilot = [c for c in candidates if c.get("role", "pilot") == "pilot"]
    todo = [c for c in pilot if needs_fetch(paths, c["qid"], refresh)]
    run.skipped_existing = len(pilot) - len(todo)
    if limit is not None:
        todo = todo[:limit]
    run.planned = len(todo)
    if dry_run:
        log(f"[dry-run] {len(pilot)} pilot titles; {run.skipped_existing} already fetched; "
            f"would fetch {len(todo)} (limit={limit}). No network calls, nothing written.")
        for c in todo[:20]:
            log(f"  would fetch {c['qid']} {c.get('title')!r} ({c.get('year')}, {c.get('bucket')})")
        if len(todo) > 20:
            log(f"  ... and {len(todo) - 20} more")
        return run
    assert fetcher is not None
    for i, c in enumerate(todo, 1):
        record = fetcher.fetch(c)
        write_json_atomic(paths.plot_file(c["qid"]), record)
        run.fetched += 1
        status = record["status"] if record["status"] == "ok" else record["skip_reason"]
        log(f"[{i}/{len(todo)}] {c['qid']} {c.get('title')!r}: {status}")
    if backfill and (limit is None or run.fetched < limit):
        budget = None if limit is None else limit - run.fetched
        run.backfilled = _backfill(paths, candidates, fetcher, budget, log)
    return run


def _backfill(
    paths: DataPaths,
    candidates: Sequence[dict[str, Any]],
    fetcher: PlotFetcher,
    budget: int | None,
    log: Callable[[str], None],
) -> int:
    """Per bucket, fetch reserves (in rank order) until the bucket has as many passing titles
    as it has pilot slots, or reserves run out."""
    fetched = 0
    by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in candidates:
        by_bucket[c["bucket"]].append(c)
    for bucket, rows in by_bucket.items():
        held = held_priority(paths, rows)
        for r in held:
            rec = existing_status(paths, r["qid"]) or {}
            log(f"WARNING for Hari: priority series {r['qid']} {r.get('title')!r} ({bucket}) "
                f"is skipped ({rec.get('skip_reason')}: {(rec.get('skip_detail') or '')[:160]}). "
                "Its pilot slot is held, not backfilled.")
        slots = sum(1 for r in rows if r["role"] == "pilot") - len(held)
        ok = sum(1 for r in rows if _is_ok(paths, r["qid"]))
        reserves = sorted(
            (r for r in rows if r["role"] == "reserve"), key=lambda r: r["bucket_rank"]
        )
        for r in reserves:
            if ok >= slots or (budget is not None and fetched >= budget):
                break
            if not needs_fetch(paths, r["qid"], False):
                continue
            record = fetcher.fetch(r)
            write_json_atomic(paths.plot_file(r["qid"]), record)
            fetched += 1
            ok += record["status"] == "ok"
            log(f"[backfill {bucket}] {r['qid']} {r.get('title')!r}: "
                f"{record['status'] if record['status'] == 'ok' else record['skip_reason']}")
    return fetched


def _is_ok(paths: DataPaths, qid: str) -> bool:
    current = existing_status(paths, qid)
    return bool(current and current.get("status") == "ok")


def held_priority(paths: DataPaths, rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pilot rows of priority series whose plot file is not ok: their slots are held, never
    filled by a reserve (DECISIONS 2026-10-02). A title not fetched yet holds nothing."""
    return [
        r for r in rows
        if r["role"] == "pilot" and r["qid"] in PRIORITY_SERIES
        and existing_status(paths, r["qid"]) is not None and not _is_ok(paths, r["qid"])
    ]


def priority_report(
    paths: DataPaths, candidates: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Every priority series, in PRIORITY_SERIES order, with its status (DECISIONS 2026-10-02:
    only "pass" counts as passing):

    - ``pass``: in the effective pilot;
    - ``skipped``: its plot file is skipped (with the reason and detail);
    - ``not_fetched``: a candidate with no plot file yet;
    - ``reserve``: passes the plot rules but is a reserve outside the effective pilot;
    - ``not_selected``: a passing pilot row left out of the effective pilot;
    - ``not_a_candidate``: not in the candidate list at all (excluded or not selected).
    """
    by_qid = {c["qid"]: c for c in candidates}
    effective = {r["qid"] for r in effective_pilot(paths, candidates)}
    out = []
    for qid, name in PRIORITY_SERIES.items():
        cand = by_qid.get(qid)
        rec = existing_status(paths, qid) if cand else None
        entry: dict[str, Any] = {"qid": qid, "title": name,
                                 "bucket": cand.get("bucket") if cand else None,
                                 "role": cand.get("role") if cand else None}
        if cand is None:
            entry["status"] = "not_a_candidate"
        elif rec is None:
            entry["status"] = "not_fetched"
        elif rec.get("status") != "ok":
            entry.update(status="skipped", skip_reason=rec.get("skip_reason"),
                         skip_detail=rec.get("skip_detail"))
        else:
            if qid in effective:
                status = "pass"
            else:
                status = "reserve" if cand.get("role") == "reserve" else "not_selected"
            entry.update(status=status, via=rec.get("via"), via_detail=rec.get("via_detail"),
                         word_count=rec.get("word_count"),
                         coverage=(rec.get("coverage") or {}).get("seasons"))
        out.append(entry)
    return out


# ---------- report ----------


def _rate(ok: int, total: int) -> dict[str, Any]:
    return {"ok": ok, "total": total, "pass_rate": round(ok / total, 3) if total else None}


def effective_pilot(paths: DataPaths, candidates: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Passing titles per bucket, pilot first then reserves by rank, capped at the bucket's
    pilot slots: the set the annotation pilot should use."""
    out: list[dict[str, Any]] = []
    for bucket in BUCKETS:
        rows = [c for c in candidates if c["bucket"] == bucket.name]
        # a skipped priority series keeps its slot: no reserve takes it (DECISIONS 2026-10-02)
        slots = sum(1 for r in rows if r["role"] == "pilot") - len(held_priority(paths, rows))
        ordered = sorted(rows, key=lambda r: (r["role"] != "pilot", r["bucket_rank"]))
        out += [r for r in ordered if _is_ok(paths, r["qid"])][:slots]
    return out


def section_checks(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Every section check in a plot file (fetcher 1.4.0): the main article's, wherever it sits,
    and each season article's."""
    out = list(rec.get("section_checks") or [])
    out += (rec.get("main_article") or {}).get("section_checks") or []
    for part in rec.get("sources") or []:
        out += part.get("section_checks") or []
    return out


def plots_report(paths: DataPaths, candidates: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Pass rates of the 150-word rule over every candidate with a plot file."""
    fetched = []
    for c in candidates:
        rec = existing_status(paths, c["qid"])
        if rec is not None:
            fetched.append((c, rec))

    def by(key: Callable[[dict[str, Any]], str]) -> dict[str, dict[str, Any]]:
        groups: dict[str, list[bool]] = defaultdict(list)
        for c, rec in fetched:
            groups[key(c)].append(rec["status"] == "ok")
        return {k: _rate(sum(v), len(v)) for k, v in sorted(groups.items())}

    reasons = Counter(rec["skip_reason"] for _, rec in fetched if rec["status"] != "ok")
    sections = Counter(rec["section"]["heading"] for _, rec in fetched if rec["status"] == "ok")
    ok_words = sorted(rec["word_count"] for _, rec in fetched if rec["status"] == "ok")
    eff = effective_pilot(paths, candidates)
    pilot_slots = {
        b.name: sum(1 for c in candidates if c["bucket"] == b.name and c["role"] == "pilot")
        for b in BUCKETS
    }
    eff_counts = Counter(r["bucket"] for r in eff)
    via_seasons = Counter(
        c["bucket"] for c, rec in fetched
        if rec["status"] == "ok" and rec.get("via") == VIA_SEASON_ARTICLES
    )
    via_episodes = Counter(
        c["bucket"] for c, rec in fetched
        if rec["status"] == "ok" and rec.get("via_detail") == VIA_DETAIL_EPISODE_TABLE
    )
    priority = priority_report(paths, candidates)
    held = [r["qid"] for b in BUCKETS
            for r in held_priority(paths, [c for c in candidates if c["bucket"] == b.name])]
    checks = [(rec, section_checks(rec)) for _, rec in fetched]
    return {
        "candidates": len(candidates),
        "fetched": len(fetched),
        "overall": _rate(sum(rec["status"] == "ok" for _, rec in fetched), len(fetched)),
        "skip_reasons": dict(sorted(reasons.items())),
        "by_media_type": by(lambda c: c["media_type"]),
        "by_region": by(lambda c: c["region"]),
        "by_language": by(language_display),
        "by_decade": by(lambda c: c["decade"]),
        "by_bucket": by(lambda c: c["bucket"]),
        "by_role": by(lambda c: c["role"]),
        "via_season_articles": {
            "total": sum(via_seasons.values()),
            "by_bucket": dict(sorted(via_seasons.items())),
        },
        # fetcher 1.5.0: the subset built from episode-table summaries
        "via_episode_tables": {
            "total": sum(via_episodes.values()),
            "by_bucket": dict(sorted(via_episodes.items())),
        },
        "priority_series": priority,
        # fetcher 1.4.0 (DECISIONS 2026-10-02): series sections with non-plot parts dropped
        "non_plot_filter": {
            "titles_with_parts_dropped": sum(
                1 for _, cs in checks if any(c.get("dropped") for c in cs)),
            "titles_with_a_section_rejected": sum(
                1 for _, cs in checks if any(c.get("accepted") is False for c in cs)),
            "partial_season_coverage": sum(
                1 for rec, _ in checks
                if rec["status"] == "ok" and (rec.get("coverage") or {}).get("partial")),
        },
        "sections_used": dict(sections.most_common()),
        "ok_word_count": (
            {"min": ok_words[0], "median": ok_words[len(ok_words) // 2], "max": ok_words[-1]}
            if ok_words else None
        ),
        "effective_pilot": {
            "total": len(eff),
            "slots": sum(pilot_slots.values()),
            "by_bucket": {b: f"{eff_counts.get(b, 0)}/{n}" for b, n in pilot_slots.items() if n},
            "held_for_priority_series": held,
        },
    }


def format_report(report: dict[str, Any]) -> str:
    lines = [
        f"Plot sections: {report['fetched']} of {report['candidates']} candidates fetched; "
        f"{report['overall']['ok']} pass the 150-word rule "
        f"({_pct(report['overall']['pass_rate'])}).",
        f"Skip reasons: {report['skip_reasons'] or 'none'}",
    ]
    for title, key in (("By type", "by_media_type"), ("By region", "by_region"),
                       ("By language", "by_language"), ("By decade", "by_decade"),
                       ("By bucket", "by_bucket")):
        lines.append(f"{title}:")
        for k, v in report[key].items():
            lines.append(f"  {k:<26} {v['ok']:>4}/{v['total']:<4} {_pct(v['pass_rate'])}")
    via = report["via_season_articles"]
    lines.append(f"Via season articles: {via['total']} passing titles")
    for bucket, n in via["by_bucket"].items():
        lines.append(f"  {bucket:<26} {n:>4}")
    episodes = report.get("via_episode_tables")
    if episodes is not None:
        lines.append(f"Via episode tables (of those): {episodes['total']} passing titles")
        for bucket, n in episodes["by_bucket"].items():
            lines.append(f"  {bucket:<26} {n:>4}")
    npf = report.get("non_plot_filter")
    if npf:
        lines.append(
            f"Non-plot filter: {npf['titles_with_parts_dropped']} titles had parts dropped, "
            f"{npf['titles_with_a_section_rejected']} had a section rejected as mostly "
            f"non-plot; {npf['partial_season_coverage']} passing series cover only some seasons")
    eff = report["effective_pilot"]
    lines.append(f"Effective pilot (after backfill): {eff['total']}/{eff['slots']} slots filled")
    lines.append("  " + ", ".join(f"{b} {v}" for b, v in eff["by_bucket"].items()))
    if eff.get("held_for_priority_series"):
        lines.append("  slots held for skipped priority series (not backfilled): "
                     + ", ".join(eff["held_for_priority_series"]))
    if "priority_series" in report:
        lines += format_priority(report["priority_series"])
    return "\n".join(lines)


def format_priority(entries: list[dict[str, Any]]) -> list[str]:
    """The priority-series check: a count, then one WARNING line per series that is not ok."""
    passing = sum(1 for e in entries if e["status"] == "pass")
    lines = [f"Priority series (DECISIONS 2026-10-02): {passing}/{len(entries)} pass "
             "(in the effective pilot)"]
    for e in entries:
        where = f" ({e['bucket']}, role {e['role']})" if e["bucket"] else ""
        if e["status"] == "skipped":
            lines.append(f"  WARNING for Hari: {e['qid']} {e['title']!r}{where} skipped: "
                         f"{e['skip_reason']}: {(e.get('skip_detail') or '')[:200]}")
        elif e["status"] == "reserve":
            lines.append(f"  WARNING for Hari: {e['qid']} {e['title']!r}{where} passes but is "
                         "a reserve, not in the effective pilot")
        elif e["status"] != "pass":
            lines.append(f"  WARNING for Hari: {e['qid']} {e['title']!r}{where} is "
                         f"{e['status'].replace('_', ' ')}")
    return lines


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:.0f}%"


__all__ = [
    "PRIORITY_SERIES",
    "PlotRun",
    "effective_pilot",
    "format_report",
    "plots_report",
    "priority_report",
    "run_plots",
]
