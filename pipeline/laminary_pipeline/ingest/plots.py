"""Fetch plot sections for the pilot candidates, resumably, and report pass rates.

One file per title: ``data/plots/<QID>.json`` with ``status`` "ok" (text + a schema-shaped
``source``) or "skipped" (with ``skip_reason``). A title with a file is not fetched again unless
``--refresh`` is given; ``fetch_error`` files (transient network problems) are always retried,
and so are series skipped as too thin before the season-article fallback existed.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from laminary_pipeline.ingest.candidates import BUCKETS, language_display
from laminary_pipeline.ingest.paths import DataPaths, read_json, write_json_atomic
from laminary_pipeline.ingest.wikipedia import (
    SKIP_FETCH_ERROR,
    SKIP_NO_SECTION,
    SKIP_TOO_SHORT,
    VIA_SEASON_ARTICLES,
    PlotFetcher,
)


def existing_status(paths: DataPaths, qid: str) -> dict[str, Any] | None:
    path = paths.plot_file(qid)
    return read_json(path) if path.exists() else None


def needs_fetch(paths: DataPaths, qid: str, refresh: bool) -> bool:
    if refresh:
        return True
    current = existing_status(paths, qid)
    return current is None or current.get("skip_reason") == SKIP_FETCH_ERROR or (
        _thin_series_before_seasons(current)
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
        slots = sum(1 for r in rows if r["role"] == "pilot")
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


# ---------- report ----------


def _rate(ok: int, total: int) -> dict[str, Any]:
    return {"ok": ok, "total": total, "pass_rate": round(ok / total, 3) if total else None}


def effective_pilot(paths: DataPaths, candidates: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Passing titles per bucket, pilot first then reserves by rank, capped at the bucket's
    pilot slots: the set the annotation pilot should use."""
    out: list[dict[str, Any]] = []
    for bucket in BUCKETS:
        rows = [c for c in candidates if c["bucket"] == bucket.name]
        slots = sum(1 for r in rows if r["role"] == "pilot")
        ordered = sorted(rows, key=lambda r: (r["role"] != "pilot", r["bucket_rank"]))
        out += [r for r in ordered if _is_ok(paths, r["qid"])][:slots]
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
        "sections_used": dict(sections.most_common()),
        "ok_word_count": (
            {"min": ok_words[0], "median": ok_words[len(ok_words) // 2], "max": ok_words[-1]}
            if ok_words else None
        ),
        "effective_pilot": {
            "total": len(eff),
            "slots": sum(pilot_slots.values()),
            "by_bucket": {b: f"{eff_counts.get(b, 0)}/{n}" for b, n in pilot_slots.items() if n},
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
    eff = report["effective_pilot"]
    lines.append(f"Effective pilot (after backfill): {eff['total']}/{eff['slots']} slots filled")
    lines.append("  " + ", ".join(f"{b} {v}" for b, v in eff["by_bucket"].items()))
    return "\n".join(lines)


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:.0f}%"


__all__ = [
    "PlotRun",
    "effective_pilot",
    "format_report",
    "plots_report",
    "run_plots",
]
