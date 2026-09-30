"""Evaluation CLI.

    python -m laminary_pipeline.evaluate --gold data/gold --run <run_id> [--run <run_id> ...]
        [--leak-flags flags.jsonl] [--pairs data/config/similarity_pairs.csv] [--out DIR]

Writes ``evaluation.md`` and ``evaluation.json``: into the run directory for a single run,
otherwise into ``--out`` (required then). ``--annotations`` takes record files instead of runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from laminary_pipeline.annotate.config import ANNOTATIONS_DIR
from laminary_pipeline.evaluate.pairs import check_pairs, load_pairs
from laminary_pipeline.evaluate.report import evaluate, load_records, render_markdown


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m laminary_pipeline.evaluate")
    p.add_argument(
        "--gold",
        type=Path,
        nargs="+",
        required=True,
        help="gold_label record files or directories (.json/.jsonl)",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--run", nargs="+", help="annotation run id(s)")
    src.add_argument("--annotations", type=Path, nargs="+", help="llm_annotation record files")
    p.add_argument("--annotations-dir", type=Path, default=ANNOTATIONS_DIR)
    p.add_argument("--leak-flags", type=Path, help="JSONL of human safe_text leak flags")
    p.add_argument("--pairs", type=Path, help="should/should-not-match pairs CSV")
    p.add_argument("--out", type=Path, help="output directory")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    gold = load_records(args.gold)
    cost_reports = []
    if args.run:
        run_dirs = [args.annotations_dir / r for r in args.run]
        model = load_records([d / "annotations.jsonl" for d in run_dirs])
        cost_reports = [
            json.loads((d / "cost.json").read_text(encoding="utf-8"))
            for d in run_dirs
            if (d / "cost.json").exists()
        ]
        out = args.out or (run_dirs[0] if len(run_dirs) == 1 else None)
    else:
        model = load_records(args.annotations)
        out = args.out
    if out is None:
        raise SystemExit("--out is required when evaluating more than one run or record files")
    flags = None
    if args.leak_flags:
        flags = [
            json.loads(line)
            for line in args.leak_flags.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    pair_report = check_pairs(load_pairs(args.pairs)) if args.pairs else None
    report = evaluate(
        gold, model, cost_reports=cost_reports, leak_flags=flags, pair_report=pair_report
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / "evaluation.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md = render_markdown(report, pair_report)
    (out / "evaluation.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"\nWrote {out / 'evaluation.md'} and {out / 'evaluation.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
