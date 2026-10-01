"""Annotation CLI.

    python -m laminary_pipeline.annotate single --title movie:603 --model M --dry-run
    python -m laminary_pipeline.annotate batch --titles-file data/pilot_effective.jsonl \\
        --model M --dry-run
    python -m laminary_pipeline.annotate batch --resume <run_id>
    python -m laminary_pipeline.annotate smoke --title movie:603 --model M --dry-run
    python -m laminary_pipeline.annotate estimate

Every command without --dry-run spends money: it needs ANTHROPIC_API_KEY and --budget-usd, the
spend Hari approved. It refuses to start if the WORST-CASE bound (cost.py) exceeds the budget,
and the runner re-checks measured spend before every request or batch round. Live batch runs
also need --titles-file, so a run is always scoped to a named set (pilot or gold).

Dry runs make no API call, need no key and write no run files (``--dump-requests`` writes one
JSONL file, under the gitignored pipeline/data directory unless a path is given).

Phase 1 runs use ``claude-opus-5-5`` only (DECISIONS.md); another model needs
``--allow-non-phase1-model``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from laminary_pipeline.annotate.client import AnnotationAPI, MissingApiKeyError, make_api
from laminary_pipeline.annotate.config import (
    ANNOTATIONS_DIR,
    APPROVAL_THRESHOLD_USD,
    BATCH_POLL_SECONDS,
    DATA_DIR,
    DEFAULT_EFFORT,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_PROMPT_VERSION,
    MAX_ATTEMPTS_CAP,
    MAX_TOKENS_CAP,
    MODELS,
    PHASE1_MODEL,
    PILOT_TITLE_CAP,
    PLOTS_DIR,
    PROMPTS_DIR,
)
from laminary_pipeline.annotate.cost import (
    ESTIMATE_METHOD,
    CostEstimate,
    estimate,
    estimate_tokens,
    format_table,
    projection_table,
    request_token_split,
)
from laminary_pipeline.annotate.inputs import (
    GateError,
    InputFormatError,
    find_plot,
    load_plots,
    read_titles_file,
    select_plots,
)
from laminary_pipeline.annotate.prompt import build_request, load_prompt
from laminary_pipeline.annotate.records import now_rfc3339
from laminary_pipeline.annotate.runner import (
    Plan,
    Prepared,
    RunConfig,
    RunDir,
    custom_id,
    existing_done_keys,
    plan_run,
    run_batch,
    run_smoke,
    run_sync,
    start_run,
)

Out = Callable[[str], None]
EXIT_STOPPED = 3  # a live run that stopped early (budget, fatal API error); see manifest


# --- argument types ------------------------------------------------------------------------


def budget_usd(value: str) -> float:
    """A finite dollar amount above 0 (rejects nan, inf, 0 and negatives)."""
    try:
        x = float(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from e
    if not math.isfinite(x) or x <= 0:
        raise argparse.ArgumentTypeError(f"must be a finite amount above 0, got {value!r}")
    return x


def bounded_int(lo: int, hi: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        try:
            n = int(value)
        except ValueError as e:
            raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from e
        if not lo <= n <= hi:
            raise argparse.ArgumentTypeError(f"must be between {lo} and {hi}, got {n}")
        return n

    return parse


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m laminary_pipeline.annotate")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser, *, model_required: bool = True) -> None:
        sp.add_argument(
            "--model",
            choices=sorted(MODELS),
            required=model_required,
            help=f"exact model id; Phase 1 is {PHASE1_MODEL} only",
        )
        sp.add_argument(
            "--allow-non-phase1-model",
            action="store_true",
            help=f"permit a model other than {PHASE1_MODEL} (prints a warning)",
        )
        sp.add_argument("--prompt-version", default=DEFAULT_PROMPT_VERSION)
        sp.add_argument(
            "--effort", default=DEFAULT_EFFORT, choices=["low", "medium", "high", "xhigh", "max"]
        )
        sp.add_argument(
            "--max-tokens",
            type=bounded_int(1, MAX_TOKENS_CAP),
            default=DEFAULT_MAX_TOKENS,
            help=f"output cap per request, at most {MAX_TOKENS_CAP}",
        )
        sp.add_argument("--plots-dir", type=Path, default=PLOTS_DIR)
        sp.add_argument("--out-dir", type=Path, default=ANNOTATIONS_DIR)
        sp.add_argument("--prompts-dir", type=Path, default=PROMPTS_DIR)
        sp.add_argument(
            "--dry-run",
            action="store_true",
            help="print the exact request, cost estimate and title count; no call",
        )
        sp.add_argument(
            "--budget-usd",
            type=budget_usd,
            help="approved spend ceiling (finite, above 0); required for live runs",
        )
        sp.add_argument(
            "--count-tokens",
            action="store_true",
            help="live: count input tokens exactly with the count_tokens endpoint",
        )

    attempts = bounded_int(1, MAX_ATTEMPTS_CAP)
    single = sub.add_parser("single", help="annotate one title synchronously (with retries)")
    common(single)
    single.add_argument("--title", required=True, help="title key (movie:603) or file stem")
    single.add_argument("--max-attempts", type=attempts, default=DEFAULT_MAX_ATTEMPTS)

    smoke = sub.add_parser("smoke", help="ONE structured-output request for ONE title")
    common(smoke)
    smoke.add_argument("--title", required=True)

    batch = sub.add_parser("batch", help="annotate many titles through the Batch API")
    common(batch, model_required=False)
    batch.add_argument(
        "--titles-file",
        type=Path,
        help="titles to annotate: pilot_effective.jsonl, gold_selection.jsonl, or one QID or "
        "title key per line. Required for live runs",
    )
    batch.add_argument("--limit", type=int, help="at most this many titles")
    batch.add_argument("--max-attempts", type=attempts, default=DEFAULT_MAX_ATTEMPTS)
    batch.add_argument("--resume", metavar="RUN_ID", help="continue an interrupted batch run")
    batch.add_argument(
        "--allow-budget-increase",
        action="store_true",
        help="resume only: accept a --budget-usd above the run's saved budget",
    )
    batch.add_argument(
        "--dump-requests",
        type=Path,
        nargs="?",
        const=Path(""),
        help="dry run: write every exact batch request to a JSONL file (default: under "
        "pipeline/data/dry_runs/)",
    )
    batch.add_argument(
        "--allow-over-pilot",
        action="store_true",
        help=f"allow more than {PILOT_TITLE_CAP} titles (after the pilot only)",
    )
    batch.add_argument("--poll-seconds", type=float, default=BATCH_POLL_SECONDS)

    est = sub.add_parser("estimate", help="offline cost projections (smoke, 100, 500 titles)")
    est.add_argument("--models", nargs="+", choices=sorted(MODELS), default=sorted(MODELS))
    est.add_argument("--prompt-version", default=DEFAULT_PROMPT_VERSION)
    est.add_argument("--prompts-dir", type=Path, default=PROMPTS_DIR)
    return p


def _check_model(args: argparse.Namespace, out: Out) -> None:
    if args.model is None or args.model == PHASE1_MODEL:
        return
    if not args.allow_non_phase1_model:
        raise SystemExit(
            f"--model {args.model} refused: Phase 1 annotates with {PHASE1_MODEL} only "
            "(DECISIONS.md). Pass --allow-non-phase1-model to override."
        )
    out(
        f"WARNING: --model {args.model} is not the Phase 1 model {PHASE1_MODEL}; records from "
        "this run are not Phase 1 annotations."
    )


def _config(args: argparse.Namespace) -> RunConfig:
    return RunConfig(
        model=args.model,
        prompt_version=args.prompt_version,
        effort=args.effort,
        max_tokens=args.max_tokens,
        max_attempts=getattr(args, "max_attempts", 1) if args.command != "smoke" else 1,
        out_root=args.out_dir,
        prompts_dir=args.prompts_dir,
    )


def _plan_estimate(
    plan: Plan, cfg: RunConfig, *, batch: bool, exact: dict[str, int] | None
) -> CostEstimate:
    splits = [request_token_split(p.params) for p in plan.todo]
    static = splits[0][0]
    variable = [v for _, v in splits]
    method = ESTIMATE_METHOD
    if exact:
        # Exact total input per request; the static part stays the char-based estimate.
        variable = [max(exact[p.key] - static, 0) for p in plan.todo]
        method = "count_tokens endpoint (exact input); output is an estimate"
    return estimate(
        cfg.model,
        len(plan.todo),
        static_tokens=static,
        variable_tokens=(min(variable), max(variable)),
        batch=batch,
        caching=True,
        token_method=method,
        max_tokens=cfg.max_tokens,
        max_attempts=cfg.max_attempts,
        per_title_variable=variable,
    )


def _print_plan(
    out: Out, mode: str, cfg: RunConfig, prompt_sha: str, plan: Plan, est: CostEstimate | None
) -> None:
    out(
        f"Mode: {mode} | model {cfg.model} | prompt {cfg.prompt_version} (sha256 {prompt_sha})"
        f" | effort {cfg.effort} | max_tokens {cfg.max_tokens} | max attempts {cfg.max_attempts}"
    )
    out(
        f"Titles to annotate: {len(plan.todo)} | skipped by the gate: {len(plan.skipped)} | "
        f"already annotated with this model, prompt and source: {len(plan.already_done)}"
    )
    for key, reason in plan.skipped:
        out(f"  skipped {key}: {reason}")
    if est is not None:
        out(
            f"Cost estimate: ${est.usd_low:.4f} to ${est.usd_high:.4f} expected "
            f"({'Batch, ' if est.batch else 'standard, '}prompt caching on); worst case "
            f"${est.usd_worst_case:.4f} ({est.worst_case_method}). "
            f"Tokens: {est.token_method}. Prices: {est.price_source}."
        )
        if est.usd_worst_case > APPROVAL_THRESHOLD_USD:
            out(
                f"NOTE: worst case over ${APPROVAL_THRESHOLD_USD:.0f}; Hari must approve this run."
            )


def _guard_spend(est: CostEstimate, budget: float | None) -> None:
    if budget is None:
        raise SystemExit(
            f"Live run refused: pass --budget-usd with the spend Hari approved "
            f"(worst case ${est.usd_worst_case:.4f})."
        )
    if est.usd_worst_case > budget:
        raise SystemExit(
            f"Live run refused: worst case ${est.usd_worst_case:.4f} exceeds --budget-usd "
            f"{budget} ({est.worst_case_method}). Expected ${est.usd_low:.4f} to "
            f"${est.usd_high:.4f}."
        )


def _exact_counts(api: AnnotationAPI, plan: Plan) -> dict[str, int]:
    return {p.key: api.count_tokens(p.params) for p in plan.todo}


def _live_estimate(
    args: argparse.Namespace, api: AnnotationAPI, plan: Plan, cfg: RunConfig, *, batch: bool
) -> CostEstimate:
    """Guard on the offline estimate FIRST (no call before the budget check), then optionally
    refine with exact counts and guard again."""
    est = _plan_estimate(plan, cfg, batch=batch, exact=None)
    _guard_spend(est, args.budget_usd)
    if args.count_tokens:
        est = _plan_estimate(plan, cfg, batch=batch, exact=_exact_counts(api, plan))
        _guard_spend(est, args.budget_usd)
    return est


def _finish(out: Out, status: str, run: RunDir) -> int:
    out(json.dumps(run.read_json("cost.json"), indent=2))
    out(f"Run files: {run.path}")
    if status != "complete":
        out(f"Run {status}")
        return EXIT_STOPPED
    return 0


def cmd_single_or_smoke(
    args: argparse.Namespace, api_factory: Callable[[], AnnotationAPI], out: Out
) -> int:
    _check_model(args, out)
    cfg = _config(args)
    prompt = load_prompt(cfg.prompt_version, cfg.prompts_dir)
    loaded = load_plots(args.plots_dir)
    try:
        plot = find_plot(loaded.plots, args.title)
    except KeyError:
        reasons = [r for o, r in loaded.unusable if args.title in o]
        out(f"No usable plot input for {args.title!r}; nothing sent. {'; '.join(reasons)}")
        return 2
    plan = plan_run([plot], cfg, prompt, done=set() if args.command == "smoke" else None)
    if plan.skipped:
        out(f"Refused by the gate, nothing sent: {plan.skipped[0][1]}")
        return 2
    if plan.already_done:
        out(f"{plot.key} is already annotated with this model, prompt and source; nothing to do.")
        return 0
    if args.dry_run:
        est = _plan_estimate(plan, cfg, batch=False, exact=None)
        out("DRY RUN: no API call made, no files written.")
        _print_plan(out, args.command, cfg, prompt.sha256, plan, est)
        out("Exact request:")
        out(json.dumps(plan.todo[0].params, indent=2, ensure_ascii=False))
        return 0
    _guard_spend(_plan_estimate(plan, cfg, batch=False, exact=None), args.budget_usd)
    api = api_factory()
    est = _live_estimate(args, api, plan, cfg, batch=False)
    _print_plan(out, args.command, cfg, prompt.sha256, plan, est)
    run = start_run(
        args.command, cfg, prompt, plan, budget_usd=args.budget_usd, cost_estimate=est.as_dict()
    )
    if args.command == "smoke":
        report = run_smoke(api, run, plan.todo[0], cfg)
        out(json.dumps(report, indent=2))
        status = report["status"]
    else:
        status = run_sync(api, run, plan.todo, cfg)
    return _finish(out, status, run)


def _resume_budget(args: argparse.Namespace, saved: float | None, out: Out) -> float:
    """The saved budget, or a lower --budget-usd. A higher one needs --allow-budget-increase."""
    given = args.budget_usd
    if saved is None and given is None:
        raise SystemExit(
            "Resume refused: this run's manifest has no saved budget; pass --budget-usd with "
            "the spend Hari approved for the whole run."
        )
    if given is None:
        return float(saved)  # type: ignore[arg-type]
    if saved is not None and given > saved:
        if not args.allow_budget_increase:
            raise SystemExit(
                f"Resume refused: --budget-usd {given} is above the run's saved budget {saved}. "
                "Pass --allow-budget-increase if Hari approved the higher amount."
            )
        out(f"WARNING: raising this run's budget from ${saved} to ${given}.")
    return given


def _resume(args: argparse.Namespace, api_factory: Callable[[], AnnotationAPI], out: Out) -> int:
    run = RunDir(args.out_dir, args.resume)
    manifest = run.read_json("manifest.json")
    budget = _resume_budget(args, manifest.get("budget_usd"), out)
    if budget != manifest.get("budget_usd"):
        history = manifest.get("budget_history", [])
        history.append({"at": now_rfc3339(), "from": manifest.get("budget_usd"), "to": budget})
        manifest.update(budget_usd=budget, budget_history=history)
        run.write_json("manifest.json", manifest)
    cfg = RunConfig(
        model=manifest["model"],
        prompt_version=manifest["prompt_version"],
        effort=manifest["effort"],
        max_tokens=manifest["max_tokens"],
        max_attempts=manifest["max_attempts"],
        out_root=args.out_dir,
        prompts_dir=args.prompts_dir,
    )
    prompt = load_prompt(cfg.prompt_version, cfg.prompts_dir)
    if prompt.sha256 != manifest["prompt_sha256"]:
        raise SystemExit("prompt text differs from the run's; refusing to resume")
    plots = {p.key: p for p in load_plots(args.plots_dir).plots}
    prepared: list[Prepared] = []
    for key, hashes in manifest["titles"].items():
        try:
            if key not in plots:
                raise GateError(f"{key}: plot input no longer present")
            gated, params = build_request(
                plots[key],
                model=cfg.model,
                prompt=prompt,
                effort=cfg.effort,
                max_tokens=cfg.max_tokens,
            )
            if list(gated.source_hashes) != hashes:
                raise GateError(f"{key}: source text changed since the run started")
        except GateError as e:
            finished = {f["title_key"] for f in run.read_jsonl("failures.jsonl")}
            if key not in finished:
                run.append(
                    "failures.jsonl",
                    {
                        "title_key": key,
                        "run_id": run.run_id,
                        "reason": str(e),
                        "retryable": True,
                        "failed_at": now_rfc3339(),
                        "attempts": [],
                    },
                )
            continue
        prepared.append(Prepared(gated, params))
    out(f"Resuming {run.run_id} with budget ${budget:.4f}.")
    status = run_batch(
        api_factory(), run, prepared, cfg, poll_seconds=args.poll_seconds, log=out
    )
    return _finish(out, status, run)


def _dump_path(args: argparse.Namespace) -> Path:
    if args.dump_requests and str(args.dump_requests) not in ("", "."):
        return args.dump_requests
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return args.out_dir.parent / "dry_runs" / f"batch_requests_{stamp}.jsonl"


def _write_dump(args: argparse.Namespace, plan: Plan, out: Out) -> None:
    path = _dump_path(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for p in plan.todo:
            line = {"custom_id": custom_id(p.key, 1), "params": p.params}
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    out(f"Wrote {len(plan.todo)} exact batch requests to {path} (the only file written).")
    try:
        path.resolve().relative_to(DATA_DIR.resolve())
    except ValueError:
        out(
            "NOTE: that file holds Wikipedia plot text and is outside the gitignored "
            "pipeline/data directory; don't commit it."
        )


def cmd_batch(args: argparse.Namespace, api_factory: Callable[[], AnnotationAPI], out: Out) -> int:
    if args.resume:
        return _resume(args, api_factory, out)
    if not args.model:
        raise SystemExit(f"--model is required ({PHASE1_MODEL} for Phase 1)")
    _check_model(args, out)
    if args.titles_file is None and not args.dry_run:
        raise SystemExit(
            "Live batch runs need --titles-file (pilot_effective.jsonl, gold_selection.jsonl "
            "or a list of QIDs/title keys), so a run is never scoped by file order."
        )
    if args.titles_file is None and args.limit is None:
        raise SystemExit("--titles-file or --limit is required")
    if args.limit is not None and args.limit > PILOT_TITLE_CAP and not args.allow_over_pilot:
        raise SystemExit(
            f"--limit over {PILOT_TITLE_CAP} needs --allow-over-pilot "
            "(pilot first; Hari approves larger runs)"
        )
    cfg = _config(args)
    prompt = load_prompt(cfg.prompt_version, cfg.prompts_dir)
    loaded = load_plots(args.plots_dir)
    plots = loaded.plots
    not_found: list[tuple[str, str]] = []
    if args.titles_file is not None:
        try:
            wanted = read_titles_file(args.titles_file)
        except InputFormatError as e:
            raise SystemExit(f"--titles-file: {e}") from e
        plots, missing = select_plots(plots, wanted)
        not_found = [(m, "listed in --titles-file but no usable plot input") for m in missing]
    if len(plots) > PILOT_TITLE_CAP and args.limit is None and not args.allow_over_pilot:
        raise SystemExit(
            f"{len(plots)} titles selected, over {PILOT_TITLE_CAP}: needs --allow-over-pilot "
            "(pilot first; Hari approves larger runs)"
        )
    plan = plan_run(plots, cfg, prompt, limit=args.limit, done=existing_done_keys(cfg.out_root))
    plan.skipped = not_found + list(loaded.unusable if args.titles_file is None else []) + (
        plan.skipped
    )
    if not plan.todo:
        _print_plan(out, "batch", cfg, prompt.sha256, plan, None)
        out("Nothing to annotate.")
        return 0
    if args.dry_run:
        est = _plan_estimate(plan, cfg, batch=True, exact=None)
        out("DRY RUN: no API call made, no run files written.")
        _print_plan(out, "batch", cfg, prompt.sha256, plan, est)
        if args.dump_requests is not None:
            _write_dump(args, plan, out)
        out(f"Exact request for the first title ({plan.todo[0].key}):")
        out(json.dumps(plan.todo[0].params, indent=2, ensure_ascii=False))
        return 0
    _guard_spend(_plan_estimate(plan, cfg, batch=True, exact=None), args.budget_usd)
    api = api_factory()
    est = _live_estimate(args, api, plan, cfg, batch=True)
    _print_plan(out, "batch", cfg, prompt.sha256, plan, est)
    run = start_run(
        "batch",
        cfg,
        prompt,
        plan,
        budget_usd=args.budget_usd,
        cost_estimate=est.as_dict(),
        titles_file=str(args.titles_file),
    )
    status = run_batch(api, run, plan.todo, cfg, poll_seconds=args.poll_seconds, log=out)
    return _finish(out, status, run)


def cmd_estimate(args: argparse.Namespace, out: Out) -> int:
    prompt = load_prompt(args.prompt_version, args.prompts_dir)
    static = estimate_tokens(prompt.body)
    out(f"Prompt {prompt.version}: ~{static:,} tokens static (cached) prefix.")
    out(format_table(projection_table(static, args.models)))
    return 0


def main(
    argv: list[str] | None = None,
    *,
    api_factory: Callable[[], AnnotationAPI] = make_api,
    out: Out = print,
) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "estimate":
            return cmd_estimate(args, out)
        if args.command == "batch":
            return cmd_batch(args, api_factory, out)
        return cmd_single_or_smoke(args, api_factory, out)
    except MissingApiKeyError as e:
        out(f"Not run: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
