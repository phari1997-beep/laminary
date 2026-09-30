"""CLI: ``python -m laminary_pipeline.ingest {candidates,plots,report}``.

    candidates [--limit N] [--dry-run] [--refresh]
        Query Wikidata and write data/pilot_candidates.jsonl (+ reports/candidates_summary.json).
        --limit N keeps N pilot titles, round-robin across buckets (for smoke runs).
        --dry-run never touches the network: it lists the queries and, if every response is
        already cached, shows the selection without writing it.
    plots [--limit N] [--dry-run] [--refresh] [--backfill] [--qid Q...]
        Fetch Wikipedia plot sections into data/plots/<QID>.json. Resumable: titles with a file
        are skipped (fetch errors are retried). --backfill fetches ranked reserves for buckets
        that lost titles to the 150-word rule.
    report
        Print and write reports/plots_summary.json: pass rates by type, region, language and
        decade.

Global options: --data-dir DIR (default pipeline/data, or $LAMINARY_DATA_DIR) and --offline
(serve only from the HTTP cache; fail on a miss).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from typing import Any

from laminary_pipeline.ingest import candidates as cand
from laminary_pipeline.ingest.http import HttpClient, OfflineCacheMiss, Transport
from laminary_pipeline.ingest.paths import (
    DataPaths,
    read_jsonl,
    write_json_atomic,
    write_jsonl_atomic,
)
from laminary_pipeline.ingest.plots import (
    effective_pilot,
    format_report,
    plots_report,
    run_plots,
)
from laminary_pipeline.ingest.wikidata import POOL_TTL, Wikidata
from laminary_pipeline.ingest.wikipedia import PlotFetcher, now_utc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m laminary_pipeline.ingest", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", default=None)
    p.add_argument("--offline", action="store_true", help="HTTP cache only; no network")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("candidates", help="build the pilot candidate list from Wikidata")
    c.add_argument("--limit", type=int, default=None)
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--refresh", action="store_true", help="ignore cached SPARQL responses")
    pl = sub.add_parser("plots", help="fetch Wikipedia plot sections")
    pl.add_argument("--limit", type=int, default=None)
    pl.add_argument("--dry-run", action="store_true")
    pl.add_argument("--refresh", action="store_true", help="re-fetch titles that have a file")
    pl.add_argument("--backfill", action="store_true", help="use reserves to refill buckets")
    pl.add_argument("--qid", nargs="*", default=None, help="only these QIDs")
    sub.add_parser("report", help="summarize plot pass rates")
    return p


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: Transport | None = None,
    clock: Callable[[], str] = now_utc,
    sleep: Callable[[float], None] | None = None,
    log: Callable[[str], None] = print,
) -> int:
    args = build_parser().parse_args(argv)
    paths = DataPaths.resolve(args.data_dir)

    def client(offline: bool) -> HttpClient:
        kwargs: dict[str, Any] = {"offline": offline or args.offline}
        if sleep is not None:
            kwargs["sleep"] = sleep
        return HttpClient(transport, paths.cache, **kwargs)

    if args.command == "candidates":
        return _candidates(args, paths, client, clock, log)
    if args.command == "plots":
        return _plots(args, paths, client, clock, log)
    return _report(paths, log)


def _candidates(
    args: argparse.Namespace, paths: DataPaths, client: Any, clock: Any, log: Any
) -> int:
    seeds = cand.load_seeds(paths.gold_seeds)
    ttl = 0 if args.refresh else POOL_TTL
    if args.dry_run:
        log(f"[dry-run] {len(cand.pool_queries())} pool queries + 1 seed lookup "
            f"({len(seeds)} seed titles) + detail queries; target {cand.TARGET_TOTAL} titles:")
        for b in cand.BUCKETS:
            log(f"  {b.name:<16} quota {b.quota:>3}" +
                (f"  decades {b.decade_quotas}" if b.decade_quotas else ""))
        wd = Wikidata(client(True), cache_ttl=None)
        try:
            items = cand.gather(wd, seeds)
        except OfflineCacheMiss:
            log("[dry-run] responses not cached yet: a real run would query query.wikidata.org. "
                "Nothing written.")
            return 0
        sel = cand.select(items, seeds, retrieved_at=clock())
        rows = cand.interleave_limit(sel.rows, args.limit) if args.limit else sel.rows
        log("[dry-run] from cache: " + json.dumps(cand.summarize(rows)))
        return 0
    wd = Wikidata(client(False), cache_ttl=ttl)
    items = cand.gather(wd, seeds)
    sel = cand.select(items, seeds, retrieved_at=clock())
    rows = cand.interleave_limit(sel.rows, args.limit) if args.limit else sel.rows
    n = write_jsonl_atomic(paths.candidates, rows)
    summary = {
        **cand.summarize(rows),
        "pool_size": sel.pool_size,
        "excluded": dict(sel.excluded),
        "seeds_missing": [
            f"{s['title']} ({s['year']}, {s['media_type']})" for s in sel.seeds_missing
        ],
        "selector_version": cand.SELECTOR_VERSION,
        "generated_at": clock(),
    }
    write_json_atomic(paths.reports / "candidates_summary.json", summary)
    log(f"wrote {n} rows to {paths.candidates}")
    log(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def _load_candidates(paths: DataPaths, log: Any) -> list[dict[str, Any]] | None:
    if not paths.candidates.exists():
        log(f"no candidate list at {paths.candidates}; run the 'candidates' command first")
        return None
    return list(read_jsonl(paths.candidates))


def _plots(args: argparse.Namespace, paths: DataPaths, client: Any, clock: Any, log: Any) -> int:
    rows = _load_candidates(paths, log)
    if rows is None:
        return 0 if args.dry_run else 2
    if args.qid:
        wanted = set(args.qid)
        rows = [{**r, "role": "pilot"} for r in rows if r["qid"] in wanted]
    fetcher = None if args.dry_run else PlotFetcher(client(False), clock=clock)
    run_plots(paths, rows, fetcher, limit=args.limit, refresh=args.refresh,
              backfill=args.backfill, dry_run=args.dry_run, log=log)
    if not args.dry_run:
        return _report(paths, log)
    return 0


def _report(paths: DataPaths, log: Any) -> int:
    rows = _load_candidates(paths, log)
    if rows is None:
        return 2
    report = plots_report(paths, rows)
    write_json_atomic(paths.reports / "plots_summary.json", report)
    effective = [
        {k: r[k] for k in ("qid", "title", "year", "media_type", "bucket", "role", "tmdb_id")}
        for r in effective_pilot(paths, rows)
    ]
    write_jsonl_atomic(paths.effective_pilot, effective)
    log(format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
