"""CLI: ``python -m laminary_pipeline.gold {select,template,import,pairs}``.

    select [--n 100]       pick gold titles -> data/gold/gold_selection.jsonl (+ summary)
    template               write the Google Sheets CSVs -> data/gold/gold_labels_*.csv, and
                           the summary texts + manifest -> data/gold/texts/ (for Drive)
    import FILLED.csv [--out PATH] [--labeled-at ISO] [--allow-partial]
                           filled sheet -> data/gold/gold_labels.jsonl (schema-valid records);
                           prints every row problem. Writes nothing on errors unless
                           --allow-partial (then only the valid rows are written).
    pairs                  check the similarity pairs and resolve them to QIDs

All offline: these commands read local files only. --data-dir as for ingest.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from laminary_pipeline.gold.importer import format_problems, import_csv
from laminary_pipeline.gold.pairs import load_pairs, resolve_pairs
from laminary_pipeline.gold.select import DEFAULT_N, select_gold, summarize_gold
from laminary_pipeline.gold.template import template_rows, write_template
from laminary_pipeline.ingest.candidates import load_seeds
from laminary_pipeline.ingest.paths import (
    DataPaths,
    read_json,
    read_jsonl,
    write_json_atomic,
    write_jsonl_atomic,
)
from laminary_pipeline.ingest.plots import effective_pilot

SELECTION_NAME = "gold_selection.jsonl"
LABELS_NAME = "gold_labels.jsonl"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m laminary_pipeline.gold", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", default=None)
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("select")
    s.add_argument("--n", type=int, default=DEFAULT_N)
    sub.add_parser("template")
    i = sub.add_parser("import")
    i.add_argument("csv", type=Path)
    i.add_argument("--out", type=Path, default=None)
    i.add_argument("--labeled-at", default=None, help="RFC 3339 time; default now")
    i.add_argument("--allow-partial", action="store_true")
    sub.add_parser("pairs")
    return p


def main(argv: Sequence[str] | None = None, *, log: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    paths = DataPaths.resolve(args.data_dir)
    if args.command == "select":
        return _select(paths, args.n, log)
    if args.command == "template":
        return _template(paths, log)
    if args.command == "import":
        return _import(paths, args, log)
    return _pairs(paths, log)


def _plot_status(paths: DataPaths, qid: str) -> bool | None:
    f = paths.plot_file(qid)
    return read_json(f).get("status") == "ok" if f.exists() else None


def _select(paths: DataPaths, n: int, log: Callable[[str], None]) -> int:
    if not paths.candidates.exists():
        log(f"no candidate list at {paths.candidates}; run ingest candidates first")
        return 2
    candidates = list(read_jsonl(paths.candidates))
    any_plots = paths.plots.exists() and any(paths.plots.glob("Q*.json"))
    pool = effective_pilot(paths, candidates) if any_plots else [
        c for c in candidates if c["role"] == "pilot"
    ]
    rows = select_gold(pool, load_seeds(paths.gold_seeds), n=n,
                       plot_ok=lambda q: _plot_status(paths, q))
    write_jsonl_atomic(paths.gold / SELECTION_NAME, rows)
    summary = summarize_gold(rows)
    write_json_atomic(paths.reports / "gold_selection_summary.json", summary)
    log(f"selected {len(rows)} gold titles -> {paths.gold / SELECTION_NAME}")
    if not any_plots:
        log("note: no plot files yet, so titles weren't checked against the 150-word rule; "
            "re-run after 'ingest plots'")
    log(json.dumps(summary, indent=2))
    return 0


def _template(paths: DataPaths, log: Callable[[str], None]) -> int:
    sel_path = paths.gold / SELECTION_NAME
    if not sel_path.exists():
        log(f"no gold selection at {sel_path}; run 'gold select' first")
        return 2
    selection = list(read_jsonl(sel_path))
    plots = {s["qid"]: read_json(paths.plot_file(s["qid"])) for s in selection
             if paths.plot_file(s["qid"]).exists()}
    rows, skipped = template_rows(selection, plots)
    written = write_template(paths.gold, rows, plots)
    for msg in skipped:
        log(f"left out {msg}")
    n_texts = len(written) - 4  # three CSV tabs and the manifest
    log(f"{len(rows)} rows in the sheet ({sum(r['label_slot'] == '2' for r in rows)} second-"
        "labeler rows); wrote " + ", ".join(str(w) for w in written[:3])
        + f", {n_texts} summary text files and {written[-1]}")
    log(f"upload the folder {paths.gold / 'texts'} to the Laminary Drive folder, next to the "
        "gold sheet; manifest.csv lists each file's sha256")
    return 0


def _import(paths: DataPaths, args: argparse.Namespace, log: Callable[[str], None]) -> int:
    result = import_csv(args.csv, annotated_at=args.labeled_at)
    if result.warnings:
        log("Warnings:\n" + format_problems(result.warnings))
    if result.errors:
        log(f"{len(result.errors)} row(s) have problems:\n" + format_problems(result.errors))
    log(f"{len(result.records)} valid record(s); {result.skipped_unlabeled} unlabeled row(s) "
        "skipped")
    if result.errors and not args.allow_partial:
        log("nothing written (fix the rows above, or use --allow-partial)")
        return 1
    out = args.out or paths.gold / LABELS_NAME
    write_jsonl_atomic(out, result.records)
    log(f"wrote {len(result.records)} gold_label records to {out}")
    return 0 if not result.errors else 1


def _pairs(paths: DataPaths, log: Callable[[str], None]) -> int:
    if not paths.similarity_pairs.exists():
        log(f"no pairs file at {paths.similarity_pairs}")
        return 1
    pairs, errors = load_pairs(paths.similarity_pairs)
    for e in errors:
        log(f"error: {e}")
    counts = {e: sum(1 for p in pairs if p.expect == e) for e in ("match", "no_match")}
    log(f"{len(pairs)} pairs: {counts['match']} should match, {counts['no_match']} should not")
    if paths.candidates.exists():
        resolved = resolve_pairs(pairs, list(read_jsonl(paths.candidates)))
        missing = [r for r in resolved if not (r["qid_a"] and r["qid_b"])]
        log(f"{len(resolved) - len(missing)} pairs have both titles in the candidate list")
        for r in missing:
            log(f"  {r['pair_id']}: not in candidates: "
                + ", ".join(t for t, q in ((r["title_a"], r["qid_a"]), (r["title_b"], r["qid_b"]))
                            if not q))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
