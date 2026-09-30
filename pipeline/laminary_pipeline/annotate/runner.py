"""Annotation runs: plan, idempotency, single/smoke (synchronous) and batch execution.

Run files, in ``pipeline/data/annotations/<run_id>/``:

- ``manifest.json``: settings, prompt hash, titles with their source hashes, batch ids per
  round, status. Written before any request is sent and updated after each step.
- ``annotations.jsonl``: validated ``llm_annotation`` records (annotated or abstained).
- ``failures.jsonl``: titles that ran out of attempts or hit a non-retryable error, with every
  attempt's reason. Never silently dropped.
- ``skipped.jsonl``: titles the gate refused before any request (reason included).
- ``attempts.jsonl``: one line per request attempt (usage, stop reason, outcome); the source of
  truth for cost and for resuming.
- ``cost.json``: measured usage priced with the (unconfirmed) price table.
- ``smoke.json``: smoke call only: latency, stop reason, usage, cost, outcome.

Idempotency: a title is skipped when any run under the annotations directory already holds a
record with the same title, model, prompt version and set of source content hashes. Failures
are not treated as done, so they are retried by the next run.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from laminary_pipeline.annotate.client import (
    AnnotationAPI,
    RequestRejectedError,
    Response,
    Usage,
)
from laminary_pipeline.annotate.config import (
    ANNOTATIONS_DIR,
    DEFAULT_EFFORT,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_PROMPT_VERSION,
    PROMPTS_DIR,
)
from laminary_pipeline.annotate.cost import price_usage
from laminary_pipeline.annotate.inputs import GatedInput, GateError, PlotInput, title_key
from laminary_pipeline.annotate.prompt import Prompt, build_request
from laminary_pipeline.annotate.records import (
    Invalid,
    RunContext,
    failure_record,
    interpret,
    now_rfc3339,
)
from laminary_pipeline.annotation import schema_version

Log = Callable[[str], None]


@dataclass
class RunConfig:
    model: str
    prompt_version: str = DEFAULT_PROMPT_VERSION
    effort: str | None = DEFAULT_EFFORT
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    out_root: Path = ANNOTATIONS_DIR
    prompts_dir: Path = PROMPTS_DIR


def new_run_id(mode: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{mode}-{secrets.token_hex(3)}"


def custom_id(key: str, round_no: int) -> str:
    """Batch custom ids allow [A-Za-z0-9_-]{1,64}: 'movie:603' round 2 -> 'movie-603-r2'."""
    return f"{key.replace(':', '-')}-r{round_no}"


# --- run directory -------------------------------------------------------------------------


class RunDir:
    def __init__(self, root: Path, run_id: str) -> None:
        self.run_id = run_id
        self.path = root / run_id

    def file(self, name: str) -> Path:
        return self.path / name

    def create(self) -> None:
        self.path.mkdir(parents=True, exist_ok=False)

    def write_json(self, name: str, obj: Any) -> None:
        tmp = self.file(name + ".tmp")
        tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.file(name))

    def read_json(self, name: str) -> Any:
        return json.loads(self.file(name).read_text(encoding="utf-8"))

    def append(self, name: str, obj: Any) -> None:
        with self.file(name).open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, sort_keys=True) + "\n")

    def read_jsonl(self, name: str) -> list[Any]:
        path = self.file(name)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --- idempotency ---------------------------------------------------------------------------

DoneKey = tuple[str, str, str, tuple[str, ...]]


def done_key(key: str, model: str, prompt_version: str, hashes: Iterable[str]) -> DoneKey:
    return (key, model, prompt_version, tuple(sorted(hashes)))


def record_done_key(record: dict[str, Any]) -> DoneKey:
    ann = record["provenance"]["annotator"]
    return done_key(
        title_key(record["title"]),
        ann["model_version"],
        ann["prompt_version"],
        (s["content_sha256"] for s in record["provenance"]["sources"]),
    )


def existing_done_keys(out_root: Path) -> set[DoneKey]:
    keys: set[DoneKey] = set()
    if not out_root.exists():
        return keys
    for path in sorted(out_root.glob("*/annotations.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                keys.add(record_done_key(json.loads(line)))
    return keys


# --- planning ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Prepared:
    gated: GatedInput
    params: dict[str, Any]

    @property
    def key(self) -> str:
        return self.gated.plot.key


@dataclass
class Plan:
    todo: list[Prepared] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (title key, gate reason)
    already_done: list[str] = field(default_factory=list)


def plan_run(
    plots: list[PlotInput],
    cfg: RunConfig,
    prompt: Prompt,
    *,
    limit: int | None = None,
    done: set[DoneKey] | None = None,
) -> Plan:
    """Gate and build every request; skip done titles; stop once ``limit`` titles are queued."""
    plan = Plan()
    done = done if done is not None else existing_done_keys(cfg.out_root)
    for plot in plots:
        if limit is not None and len(plan.todo) >= limit:
            break
        try:
            gated, params = build_request(
                plot, model=cfg.model, prompt=prompt, effort=cfg.effort, max_tokens=cfg.max_tokens
            )
        except GateError as e:
            plan.skipped.append((plot.key, str(e)))
            continue
        if done_key(plot.key, cfg.model, cfg.prompt_version, gated.source_hashes) in done:
            plan.already_done.append(plot.key)
            continue
        plan.todo.append(Prepared(gated, params))
    return plan


def start_run(mode: str, cfg: RunConfig, prompt: Prompt, plan: Plan, **extra: Any) -> RunDir:
    run = RunDir(cfg.out_root, new_run_id(mode))
    run.create()
    run.write_json(
        "manifest.json",
        {
            "run_id": run.run_id,
            "mode": mode,
            "status": "started",
            "created_at": now_rfc3339(),
            "schema_version": schema_version(),
            "model": cfg.model,
            "prompt_version": cfg.prompt_version,
            "prompt_sha256": prompt.sha256,
            "effort": cfg.effort,
            "max_tokens": cfg.max_tokens,
            "max_attempts": cfg.max_attempts,
            "titles": {p.key: list(p.gated.source_hashes) for p in plan.todo},
            "rounds": [],
            **extra,
        },
    )
    for key, reason in plan.skipped:
        run.append("skipped.jsonl", {"title_key": key, "reason": reason, "at": now_rfc3339()})
    return run


def _set_manifest(run: RunDir, **changes: Any) -> dict[str, Any]:
    manifest = run.read_json("manifest.json")
    manifest.update(changes)
    run.write_json("manifest.json", manifest)
    return manifest


# --- attempt bookkeeping -------------------------------------------------------------------


@dataclass
class TitleState:
    prepared: Prepared
    usage: Usage = field(default_factory=Usage)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    last_invalid: Invalid | None = None
    finished: bool = False


def _log_attempt(
    run: RunDir,
    state: TitleState,
    *,
    round_no: int,
    response: Response | None,
    outcome: dict[str, Any] | Invalid,
    extra: dict[str, Any] | None = None,
) -> None:
    entry = {
        "title_key": state.prepared.key,
        "attempt": len(state.attempts) + 1,
        "round": round_no,
        "at": now_rfc3339(),
        "usage": response.usage.as_dict() if response else Usage().as_dict(),
        "stop_reason": response.stop_reason if response else None,
        "request_id": response.request_id if response else None,
        "ok": not isinstance(outcome, Invalid),
        "reason": outcome.reason if isinstance(outcome, Invalid) else None,
        "retryable": outcome.retryable if isinstance(outcome, Invalid) else None,
        "raw_output_excerpt": outcome.raw_excerpt if isinstance(outcome, Invalid) else None,
        **(extra or {}),
    }
    state.attempts.append({k: v for k, v in entry.items() if k != "raw_output_excerpt"})
    run.append("attempts.jsonl", entry)


def _settle(
    run: RunDir,
    state: TitleState,
    ctx: RunContext,
    outcome: dict[str, Any] | Invalid,
    max_attempts: int,
) -> None:
    """Store a record, or a failure once the title can't or won't be retried."""
    if not isinstance(outcome, Invalid):
        run.append("annotations.jsonl", outcome)
        state.finished = True
        return
    state.last_invalid = outcome
    if not outcome.retryable or len(state.attempts) >= max_attempts:
        failure = failure_record(state.prepared.gated, ctx, state.attempts, outcome)
        run.append("failures.jsonl", failure)
        state.finished = True


def _context(run: RunDir, cfg: RunConfig, *, batch: bool) -> RunContext:
    return RunContext(
        run_id=run.run_id,
        model=cfg.model,
        prompt_version=cfg.prompt_version,
        effort=cfg.effort,
        batch=batch,
    )


# --- synchronous: single title and smoke ---------------------------------------------------


def _attempt_sync(
    api: AnnotationAPI, run: RunDir, state: TitleState, ctx: RunContext, cfg: RunConfig
) -> tuple[Response | None, dict[str, Any] | Invalid, float]:
    started = time.monotonic()
    try:
        response = api.create(state.prepared.params)
    except RequestRejectedError as e:
        outcome: dict[str, Any] | Invalid = Invalid(f"request rejected: {e}", retryable=False)
        latency = time.monotonic() - started
        _log_attempt(
            run,
            state,
            round_no=len(state.attempts) + 1,
            response=None,
            outcome=outcome,
            extra={"latency_s": round(latency, 3)},
        )
        return None, outcome, latency
    latency = time.monotonic() - started
    state.usage = state.usage + response.usage
    outcome = interpret(
        response,
        state.prepared.gated,
        ctx,
        usage_so_far=state.usage,
        attempts=len(state.attempts) + 1,
    )
    _log_attempt(
        run,
        state,
        round_no=len(state.attempts) + 1,
        response=response,
        outcome=outcome,
        extra={"latency_s": round(latency, 3)},
    )
    return response, outcome, latency


def run_sync(api: AnnotationAPI, run: RunDir, prepared: list[Prepared], cfg: RunConfig) -> None:
    """Annotate titles one request at a time (full price). Retries invalid output."""
    ctx = _context(run, cfg, batch=False)
    for p in prepared:
        state = TitleState(p)
        while not state.finished:
            _, outcome, _ = _attempt_sync(api, run, state, ctx, cfg)
            _settle(run, state, ctx, outcome, cfg.max_attempts)
    _set_manifest(run, status="complete", finished_at=now_rfc3339())
    write_cost_report(run, cfg.model)


def run_smoke(api: AnnotationAPI, run: RunDir, prepared: Prepared, cfg: RunConfig) -> dict:
    """Exactly one request for one title; no retry. Records latency, usage, outcome."""
    ctx = _context(run, cfg, batch=False)
    state = TitleState(prepared)
    response, outcome, latency = _attempt_sync(api, run, state, ctx, cfg)
    _settle(run, state, ctx, outcome, max_attempts=1)
    usage = response.usage if response else Usage()
    report = {
        "title_key": prepared.key,
        "model": cfg.model,
        "served_model": response.model if response else None,
        "latency_s": round(latency, 3),
        "stop_reason": response.stop_reason if response else None,
        "request_id": response.request_id if response else None,
        "usage": usage.as_dict(),
        "cost_usd": round(price_usage(cfg.model, usage, batch=False), 5),
        "schema_accepted": response is not None,
        "valid_record": not isinstance(outcome, Invalid),
        "reason": outcome.reason if isinstance(outcome, Invalid) else None,
    }
    run.write_json("smoke.json", report)
    _set_manifest(run, status="complete", finished_at=now_rfc3339())
    write_cost_report(run, cfg.model)
    return report


# --- batch ---------------------------------------------------------------------------------


def wait_for_batch(
    api: AnnotationAPI,
    batch_id: str,
    *,
    poll_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    log: Log = print,
    max_polls: int | None = None,
) -> None:
    polls = 0
    while True:
        status = api.batch_status(batch_id)
        if status.processing_status == "ended":
            log(f"batch {batch_id} ended: {status.counts}")
            return
        polls += 1
        if max_polls is not None and polls >= max_polls:
            raise TimeoutError(f"batch {batch_id} still {status.processing_status}; resume later")
        log(f"batch {batch_id} {status.processing_status}: {status.counts}")
        sleep(poll_seconds)


def _collect_round(
    api: AnnotationAPI,
    run: RunDir,
    round_info: dict[str, Any],
    states: dict[str, TitleState],
    ctx: RunContext,
    cfg: RunConfig,
    log: Log,
) -> None:
    ids: dict[str, str] = round_info["custom_ids"]
    seen: set[str] = set()
    for res in api.batch_results(round_info["batch_id"]):
        key = ids.get(res.custom_id)
        if key is None or res.custom_id in seen:
            log(f"ignoring unexpected batch result {res.custom_id!r}")
            continue
        seen.add(res.custom_id)
        state = states[key]
        response = res.response
        outcome: dict[str, Any] | Invalid
        if res.kind == "succeeded" and response is not None:
            state.usage = state.usage + response.usage
            outcome = interpret(
                response,
                state.prepared.gated,
                ctx,
                usage_so_far=state.usage,
                attempts=len(state.attempts) + 1,
            )
        elif res.kind == "errored":
            outcome = Invalid(
                f"batch errored: {res.error_type}: {res.error_message}",
                retryable=res.error_type != "invalid_request_error",
            )
        else:
            outcome = Invalid(f"batch result {res.kind}", retryable=True)
        _log_attempt(run, state, round_no=round_info["round"], response=response, outcome=outcome)
        _settle(run, state, ctx, outcome, cfg.max_attempts)
    for cid, key in ids.items():
        if cid not in seen and not states[key].finished:
            outcome = Invalid("no result returned for this request", retryable=True)
            _log_attempt(
                run, states[key], round_no=round_info["round"], response=None, outcome=outcome
            )
            _settle(run, states[key], ctx, outcome, cfg.max_attempts)


def _restore_states(run: RunDir, prepared: list[Prepared]) -> dict[str, TitleState]:
    """Rebuild per-title state from the run's own files (for resume)."""
    states = {p.key: TitleState(p) for p in prepared}
    for entry in run.read_jsonl("attempts.jsonl"):
        state = states.get(entry["title_key"])
        if state is None:
            continue
        state.usage = state.usage + Usage(**entry["usage"])
        state.attempts.append({k: v for k, v in entry.items() if k != "raw_output_excerpt"})
        if not entry["ok"]:
            state.last_invalid = Invalid(
                entry["reason"], bool(entry["retryable"]), entry.get("raw_output_excerpt")
            )
    finished = {title_key(r["title"]) for r in run.read_jsonl("annotations.jsonl")}
    finished |= {f["title_key"] for f in run.read_jsonl("failures.jsonl")}
    for key in finished & states.keys():
        states[key].finished = True
    return states


def run_batch(
    api: AnnotationAPI,
    run: RunDir,
    prepared: list[Prepared],
    cfg: RunConfig,
    *,
    poll_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    log: Log = print,
    max_polls: int | None = None,
) -> None:
    """Submit, poll, collect; resubmit retryable failures as a new batch, up to max_attempts
    rounds. Safe to call again on the same run after an interruption (resume)."""
    ctx = _context(run, cfg, batch=True)
    states = _restore_states(run, prepared)
    manifest = run.read_json("manifest.json")
    rounds: list[dict[str, Any]] = manifest["rounds"]
    while True:
        if rounds and not rounds[-1]["collected"]:
            current = rounds[-1]
            if current["batch_id"] is None:
                raise RuntimeError(
                    f"round {current['round']} may or may not have been submitted (the run "
                    "stopped during submission). Check the batches in the Anthropic Console "
                    "before resuming, so nothing is paid for twice."
                )
            wait_for_batch(
                api,
                current["batch_id"],
                poll_seconds=poll_seconds,
                sleep=sleep,
                log=log,
                max_polls=max_polls,
            )
            _collect_round(api, run, current, states, ctx, cfg, log)
            current["collected"] = True
            _set_manifest(run, rounds=rounds)
        pending = [s for s in states.values() if not s.finished]
        if not pending:
            break
        round_no = len(rounds) + 1
        if round_no > cfg.max_attempts:  # defensive; _settle closes titles at max_attempts
            for s in pending:
                _settle(run, s, ctx, s.last_invalid or Invalid("out of attempts", False), 0)
            break
        ids = {custom_id(s.prepared.key, round_no): s.prepared.key for s in pending}
        requests = [(cid, states[key].prepared.params) for cid, key in ids.items()]
        rounds.append({"round": round_no, "batch_id": None, "custom_ids": ids, "collected": False})
        _set_manifest(run, rounds=rounds, status=f"round {round_no} submitting")
        batch_id = api.submit_batch(requests)
        rounds[-1]["batch_id"] = batch_id
        _set_manifest(run, rounds=rounds, status=f"round {round_no} submitted")
        log(f"round {round_no}: submitted {len(requests)} requests as batch {batch_id}")
    _set_manifest(run, status="complete", finished_at=now_rfc3339())
    write_cost_report(run, cfg.model)


# --- cost report ---------------------------------------------------------------------------


def write_cost_report(run: RunDir, model: str) -> dict[str, Any]:
    """Measured usage per title (all attempts, failures included), priced offline."""
    manifest = run.read_json("manifest.json")
    batch = manifest["mode"] == "batch"
    per_title: dict[str, Usage] = {}
    for entry in run.read_jsonl("attempts.jsonl"):
        key = entry["title_key"]
        per_title[key] = per_title.get(key, Usage()) + Usage(**entry["usage"])
    total = sum(per_title.values(), Usage())
    annotated = len(run.read_jsonl("annotations.jsonl"))
    total_usd = price_usage(model, total, batch=batch)
    report = {
        "run_id": run.run_id,
        "model": model,
        "batch": batch,
        "titles_attempted": len(per_title),
        "records_stored": annotated,
        "failures": len(run.read_jsonl("failures.jsonl")),
        "requests": len(run.read_jsonl("attempts.jsonl")),
        "usage_total": total.as_dict(),
        "usd_total": round(total_usd, 5),
        "usd_per_title_attempted": round(total_usd / len(per_title), 5) if per_title else None,
        "usd_per_record_stored": round(total_usd / annotated, 5) if annotated else None,
        "per_title_usd": {
            k: round(price_usage(model, u, batch=batch), 6) for k, u in sorted(per_title.items())
        },
        "pricing": "measured tokens x the UNCONFIRMED price table in annotate/config.py",
    }
    run.write_json("cost.json", report)
    return report
