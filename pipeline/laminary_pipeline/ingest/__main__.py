"""CLI: ``python -m laminary_pipeline.ingest {candidates,plots,report,pairs}``.

    candidates [--limit N] [--dry-run] [--refresh]
        Query Wikidata and write data/pilot_candidates.jsonl (+ reports/candidates_summary.json).
        --limit N keeps N pilot titles, round-robin across buckets (for smoke runs).
        --dry-run never touches the network: it lists the queries and, if every response is
        already cached, shows the selection without writing it.
    plots [--limit N] [--dry-run] [--refresh] [--backfill] [--qid Q...] [--pairs]
        Fetch Wikipedia plot sections into data/plots/<QID>.json. Resumable: titles with a file
        are skipped (fetch errors are retried). --backfill fetches ranked reserves for buckets
        that lost titles to the 150-word rule, except for a skipped priority series
        (plots.PRIORITY_SERIES): its slot is held and a warning printed. The report that
        follows lists any priority series that ended skipped, with the reason.
        --pairs fetches the pairs set (data/pairs_candidates.jsonl) instead of the candidates,
        with the same fetcher and rules, and prints the pairs report (reports/pairs_summary.json)
        instead of the pilot report: plots_summary.json and pilot_effective.jsonl are not
        rewritten. --qid narrows it; --backfill is refused.
    report
        Print and write reports/plots_summary.json: pass rates by type, region, language and
        decade.
    pairs [--dry-run]
        Resolve the similarity-pair titles (data/config/similarity_pairs.csv) that aren't
        candidates to Wikidata QIDs (label/alias lookup + the candidate detail query) and write
        data/pairs_candidates.jsonl (role "pairs"), data/similarity_pairs_resolved.csv (QIDs, in
        the evaluate/pairs.py format) and reports/pairs_summary.json. Ambiguous, unresolved and
        ineligible titles are reported, never guessed. Never writes the candidate list, the
        effective pilot or the gold files. --dry-run reads the HTTP cache only, writes nothing.

Global options: --data-dir DIR (default pipeline/data, or $LAMINARY_DATA_DIR) and --offline
(serve only from the HTTP cache; fail on a miss).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from typing import Any

from laminary_pipeline.gold.pairs import load_pairs
from laminary_pipeline.ingest import candidates as cand
from laminary_pipeline.ingest import pairs as pairs_set
from laminary_pipeline.ingest.http import HttpClient, OfflineCacheMiss, Transport
from laminary_pipeline.ingest.paths import (
    DataPaths,
    read_jsonl,
    write_json_atomic,
    write_jsonl_atomic,
    write_text_atomic,
)
from laminary_pipeline.ingest.plots import (
    effective_pilot,
    format_report,
    not_annotatable,
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
    pl.add_argument("--pairs", action="store_true",
                    help="fetch the pairs set (data/pairs_candidates.jsonl), not the candidates")
    sub.add_parser("report", help="summarize plot pass rates")
    pr = sub.add_parser("pairs", help="resolve similarity-pair titles into the pairs set")
    pr.add_argument("--dry-run", action="store_true")
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
        if args.pairs:
            return _plots_pairs(args, paths, client, clock, log)
        return _plots(args, paths, client, clock, log)
    if args.command == "pairs":
        return _pairs(args, paths, client, clock, log)
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


def _plots_pairs(
    args: argparse.Namespace, paths: DataPaths, client: Any, clock: Any, log: Any
) -> int:
    if args.backfill:
        log("--backfill does not apply to the pairs set (it has no buckets to refill)")
        return 2
    if not paths.pairs_candidates.exists():
        log(f"no pairs set at {paths.pairs_candidates}; run the 'pairs' command first")
        return 0 if args.dry_run else 2
    rows = list(read_jsonl(paths.pairs_candidates))
    if args.qid:
        wanted = set(args.qid)
        rows = [r for r in rows if r["qid"] in wanted]
    # run_plots fetches pilot-role rows only; the pairs set's own role stays in the file
    rows = [{**r, "role": "pilot"} for r in rows]
    fetcher = None if args.dry_run else PlotFetcher(client(False), clock=clock)
    run_plots(paths, rows, fetcher, limit=args.limit, refresh=args.refresh, backfill=False,
              dry_run=args.dry_run, log=log)
    if args.dry_run:
        return 0
    return _pairs_report(paths, client, log)


def _pairs_inputs(paths: DataPaths, log: Any) -> tuple[list[Any], list[dict[str, Any]]] | None:
    if not paths.similarity_pairs.exists():
        log(f"no pairs file at {paths.similarity_pairs}")
        return None
    pairs, errors = load_pairs(paths.similarity_pairs)
    for e in errors:
        log(f"error in {paths.similarity_pairs.name}: {e}")
    if errors:
        return None
    candidates = _load_candidates(paths, log)
    if candidates is None:
        return None
    return pairs, candidates


def _effective_qids(paths: DataPaths, log: Any) -> set[str]:
    if not paths.effective_pilot.exists():
        log(f"no effective pilot at {paths.effective_pilot}: every candidate pair title joins "
            "the pairs set")
        return set()
    return {r["qid"] for r in read_jsonl(paths.effective_pilot)}


def _pairs(args: argparse.Namespace, paths: DataPaths, client: Any, clock: Any, log: Any) -> int:
    inputs = _pairs_inputs(paths, log)
    if inputs is None:
        return 2
    pairs, candidates = inputs
    # Only titles the candidate list lacks are looked up on Wikidata.
    keys, pinned = pairs_set.to_look_up(pairs, candidates)
    try:
        items = pairs_set.gather(Wikidata(client(args.dry_run), cache_ttl=POOL_TTL), keys,
                                 pinned)
    except OfflineCacheMiss:
        log(f"[dry-run] {len(keys)} pair titles need a Wikidata lookup; responses not cached "
            "yet. Nothing written.")
        return 0
    built = pairs_set.build(pairs, candidates, items,
                            in_pilot=_effective_qids(paths, log).__contains__,
                            retrieved_at=clock())
    scored = pairs_set.scorability(pairs, built.qids, lambda q: not_annotatable(paths, q),
                                    built.excluded)
    summary = pairs_set.summary(built, scored)
    if args.dry_run:
        log("[dry-run] nothing written.")
        log(pairs_set.format_summary(summary))
        return 0
    n = write_jsonl_atomic(paths.pairs_candidates, built.rows)
    resolved = pairs_set.resolved_pairs(pairs, built.qids)
    write_text_atomic(paths.pairs_resolved, pairs_set.resolved_csv(resolved))
    write_json_atomic(paths.reports / "pairs_summary.json",
                      {**summary, "generated_at": clock()})
    log(f"wrote {n} rows to {paths.pairs_candidates}; {len(resolved)} resolved pairs to "
        f"{paths.pairs_resolved}")
    log(pairs_set.format_summary(summary))
    return 0


def _pairs_report(paths: DataPaths, client: Any, log: Any) -> int:
    """After a pairs fetch: the same resolution, served from the HTTP cache (no network), with
    scorability from the plot files now present. The pairs set on disk is kept as written."""
    inputs = _pairs_inputs(paths, log)
    if inputs is None:
        return 2
    pairs, candidates = inputs
    rows = list(read_jsonl(paths.pairs_candidates))
    keys, pinned = pairs_set.to_look_up(pairs, candidates)
    try:
        items = pairs_set.gather(Wikidata(client(True), cache_ttl=None), keys, pinned)
    except OfflineCacheMiss:
        log("Wikidata lookups not in the HTTP cache: title statuses come from the pairs set "
            "only (titles left out of it show as unresolved)")
        items = {}
    items.update({r["qid"]: r for r in rows if r.get("candidate_role") is None})
    built = pairs_set.build(pairs, candidates, items,
                            in_pilot=_effective_qids(paths, log).__contains__)
    built.rows = rows
    scored = pairs_set.scorability(pairs, built.qids, lambda q: not_annotatable(paths, q),
                                    built.excluded)
    summary = pairs_set.summary(built, scored)
    fetched = [{"qid": r["qid"], "title": r["title"], "pair_ids": r["pair_ids"],
                "annotatable": not_annotatable(paths, r["qid"]) is None,
                "reason": not_annotatable(paths, r["qid"])} for r in rows]
    summary["pairs_set_plots"] = {
        "rows": len(rows),
        "fetched": sum(1 for r in rows if paths.plot_file(r["qid"]).exists()),
        "annotatable": sum(1 for f in fetched if f["annotatable"]),
        "titles": fetched,
    }
    write_json_atomic(paths.reports / "pairs_summary.json", summary)
    p = summary["pairs_set_plots"]
    log(f"Pairs set plots: {p['fetched']}/{p['rows']} fetched, {p['annotatable']} annotatable")
    for f in fetched:
        if not f["annotatable"]:
            log(f"  not annotatable: {f['qid']} {f['title']!r}: {(f['reason'] or '')[:160]}")
    log(pairs_set.format_summary(summary))
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
