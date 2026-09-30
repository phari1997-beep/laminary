"""Annotation CLI.

    python -m laminary_pipeline.annotate single --title movie:603 --model M --dry-run
    python -m laminary_pipeline.annotate batch --limit 100 --model M --dry-run
    python -m laminary_pipeline.annotate batch --resume <run_id>
    python -m laminary_pipeline.annotate smoke --title movie:603 --model M --dry-run
    python -m laminary_pipeline.annotate estimate

Every command without --dry-run spends money: it needs ANTHROPIC_API_KEY and --budget-usd, the
spend Hari approved, and refuses to start if the high estimate exceeds it. Dry runs make no
API call, need no key and write nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from laminary_pipeline.annotate.client import AnnotationAPI, MissingApiKeyError, make_api
from laminary_pipeline.annotate.config import (
    ANNOTATIONS_DIR,
    APPROVAL_THRESHOLD_USD,
    BATCH_POLL_SECONDS,
    DEFAULT_EFFORT,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_PROMPT_VERSION,
    MODELS,
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
from laminary_pipeline.annotate.inputs import GateError, find_plot, load_plots
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


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m laminary_pipeline.annotate")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser, *, model_required: bool = True) -> None:
        sp.add_argument(
            "--model",
            choices=sorted(MODELS),
            required=model_required,
            help="exact model id (Hari chooses; no default)",
        )
        sp.add_argument("--prompt-version", default=DEFAULT_PROMPT_VERSION)
        sp.add_argument(
            "--effort", default=DEFAULT_EFFORT, choices=["low", "medium", "high", "xhigh", "max"]
        )
        sp.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
        sp.add_argument("--plots-dir", type=Path, default=PLOTS_DIR)
        sp.add_argument("--out-dir", type=Path, default=ANNOTATIONS_DIR)
        sp.add_argument("--prompts-dir", type=Path, default=PROMPTS_DIR)
        sp.add_argument(
            "--dry-run",
            action="store_true",
            help="print the exact request, cost estimate and title count; no call",
        )
        sp.add_argument(
            "--budget-usd", type=float, help="approved spend ceiling; required for live runs"
        )
        sp.add_argument(
            "--count-tokens",
            action="store_true",
            help="live: count input tokens exactly with the count_tokens endpoint",
        )

    single = sub.add_parser("single", help="annotate one title synchronously (with retries)")
    common(single)
    single.add_argument("--title", required=True, help="title key (movie:603) or file stem")
    single.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)

    smoke = sub.add_parser("smoke", help="ONE structured-output request for ONE title")
    common(smoke)
    smoke.add_argument("--title", required=True)

    batch = sub.add_parser("batch", help="annotate many titles through the Batch API")
    common(batch, model_required=False)
    batch.add_argument("--limit", type=int, help="number of titles to send")
    batch.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    batch.add_argument("--resume", metavar="RUN_ID", help="continue an interrupted batch run")
    batch.add_argument(
        "--dump-requests",
        type=Path,
        help="dry run: write every exact batch request to this JSONL file",
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
            f"Cost estimate: ${est.usd_low:.4f} to ${est.usd_high:.4f} "
            f"({'Batch, ' if est.batch else 'standard, '}prompt caching on). "
            f"Tokens: {est.token_method}. Prices: {est.price_source}."
        )
        if est.usd_high > APPROVAL_THRESHOLD_USD:
            out(f"NOTE: over ${APPROVAL_THRESHOLD_USD:.0f}; Hari must approve this run.")


def _guard_spend(est: CostEstimate, budget: float | None) -> None:
    if budget is None:
        raise SystemExit(
            f"Live run refused: pass --budget-usd with the spend Hari approved "
            f"(estimate up to ${est.usd_high:.4f})."
        )
    if est.usd_high > budget:
        raise SystemExit(
            f"Live run refused: estimate up to ${est.usd_high:.4f} exceeds --budget-usd {budget}."
        )


def _exact_counts(api: AnnotationAPI, plan: Plan) -> dict[str, int]:
    return {p.key: api.count_tokens(p.params) for p in plan.todo}


def cmd_single_or_smoke(
    args: argparse.Namespace, api_factory: Callable[[], AnnotationAPI], out: Out
) -> int:
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
    api = None if args.dry_run else api_factory()
    exact = _exact_counts(api, plan) if (api and args.count_tokens) else None
    est = _plan_estimate(plan, cfg, batch=False, exact=exact)
    if args.dry_run:
        out("DRY RUN: no API call made, nothing written.")
        _print_plan(out, args.command, cfg, prompt.sha256, plan, est)
        out("Exact request:")
        out(json.dumps(plan.todo[0].params, indent=2, ensure_ascii=False))
        return 0
    _guard_spend(est, args.budget_usd)
    _print_plan(out, args.command, cfg, prompt.sha256, plan, est)
    run = start_run(args.command, cfg, prompt, plan, cost_estimate=est.as_dict())
    if args.command == "smoke":
        report = run_smoke(api, run, plan.todo[0], cfg)
        out(json.dumps(report, indent=2))
    else:
        run_sync(api, run, plan.todo, cfg)
    out(f"Run files: {run.path}")
    return 0


def _resume(args: argparse.Namespace, api_factory: Callable[[], AnnotationAPI], out: Out) -> int:
    run = RunDir(args.out_dir, args.resume)
    manifest = run.read_json("manifest.json")
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
    run_batch(api_factory(), run, prepared, cfg, poll_seconds=args.poll_seconds, log=out)
    out(json.dumps(run.read_json("cost.json"), indent=2))
    return 0


def cmd_batch(args: argparse.Namespace, api_factory: Callable[[], AnnotationAPI], out: Out) -> int:
    if args.resume:
        return _resume(args, api_factory, out)
    if not args.model:
        raise SystemExit("--model is required (Hari chooses the model)")
    if args.limit is None:
        raise SystemExit("--limit is required")
    if args.limit > PILOT_TITLE_CAP and not args.allow_over_pilot:
        raise SystemExit(
            f"--limit over {PILOT_TITLE_CAP} needs --allow-over-pilot "
            "(pilot first; Hari approves larger runs)"
        )
    cfg = _config(args)
    prompt = load_prompt(cfg.prompt_version, cfg.prompts_dir)
    loaded = load_plots(args.plots_dir)
    plan = plan_run(
        loaded.plots, cfg, prompt, limit=args.limit, done=existing_done_keys(cfg.out_root)
    )
    plan.skipped = list(loaded.unusable) + plan.skipped
    if not plan.todo:
        _print_plan(out, "batch", cfg, prompt.sha256, plan, None)
        out("Nothing to annotate.")
        return 0
    api = None if args.dry_run else api_factory()
    exact = _exact_counts(api, plan) if (api and args.count_tokens) else None
    est = _plan_estimate(plan, cfg, batch=True, exact=exact)
    if args.dry_run:
        out("DRY RUN: no API call made, nothing written.")
        _print_plan(out, "batch", cfg, prompt.sha256, plan, est)
        if args.dump_requests:
            with args.dump_requests.open("w", encoding="utf-8") as f:
                for p in plan.todo:
                    f.write(
                        json.dumps(
                            {"custom_id": custom_id(p.key, 1), "params": p.params},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            out(f"Wrote {len(plan.todo)} exact batch requests to {args.dump_requests}")
        out(f"Exact request for the first title ({plan.todo[0].key}):")
        out(json.dumps(plan.todo[0].params, indent=2, ensure_ascii=False))
        return 0
    _guard_spend(est, args.budget_usd)
    _print_plan(out, "batch", cfg, prompt.sha256, plan, est)
    run = start_run("batch", cfg, prompt, plan, cost_estimate=est.as_dict())
    run_batch(api, run, plan.todo, cfg, poll_seconds=args.poll_seconds, log=out)
    out(json.dumps(run.read_json("cost.json"), indent=2))
    out(f"Run files: {run.path}")
    return 0


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
