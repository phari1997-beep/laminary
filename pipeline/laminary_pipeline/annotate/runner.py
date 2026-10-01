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
- ``cost.json``: measured usage priced with the (unconfirmed) price table, plus the
  worst-case reserve for failed requests that may have been billed.
- ``smoke.json``: smoke call only: latency, stop reason, usage, cost, outcome.

Budget: ``manifest.json`` holds ``budget_usd``. Before every request (synchronous) or every
batch round, the runner refuses to send if measured spend so far (attempts.jsonl priced, plus
reserves) plus the worst case of what it is about to send would exceed the budget. It then stops
cleanly: manifest ``status`` starts with ``stopped:`` and lists the unfinished titles, and
cost.json is written. Fatal API errors stop the run the same way.

Locking: run_batch, run_sync and run_smoke hold an exclusive ``fcntl.flock`` on the run
directory (``run_lock``; ``<run>/run.lock`` only notes the holder) for their whole duration
(the CLI's resume holds it from before it reads the manifest). A second process on the same
run refuses to start (RunLockedError) instead of building its own spend tracker from a stale
view of the run. The OS releases the lock when the holder exits or dies. Per-title state, the
manifest and attempts.jsonl are read only after the lock is held.

Idempotency: a title is skipped when any run under the annotations directory already holds a
record with the same title, model, prompt version and set of source content hashes. Failures
are not treated as done, so they are retried by the next run.
"""

from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import secrets
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from laminary_pipeline.annotate.client import (
    AnnotationAPI,
    FatalAPIError,
    RequestRejectedError,
    Response,
    TransientAPIError,
    Usage,
)
from laminary_pipeline.annotate.config import (
    ANNOTATIONS_DIR,
    DEFAULT_EFFORT,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_PROMPT_VERSION,
    MAX_CONSECUTIVE_TRANSIENT_ERRORS,
    PROMPTS_DIR,
    TRANSIENT_BACKOFF_CAP_SECONDS,
    TRANSIENT_BACKOFF_SECONDS,
)
from laminary_pipeline.annotate.cost import (
    EXACT_COUNT_MARGIN,
    price_usage,
    request_token_split,
    worst_case_request_usd,
)
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


class RunLockedError(RuntimeError):
    """Another process holds this run's lock."""


class RunDir:
    def __init__(self, root: Path, run_id: str) -> None:
        self.run_id = run_id
        self.path = root / run_id
        self._lock_fd: int | None = None  # set while this handle holds the run lock

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


LOCK_INFO = "run.lock"


@contextmanager
def run_lock(run: RunDir) -> Iterator[None]:
    """Hold an exclusive, non-blocking flock on the run directory itself.

    The lock is on the directory's descriptor, not on a file inside it, so deleting
    ``run.lock`` by hand can't let a second process in: a lock file can be unlinked while
    held, and the next process would create and lock a fresh file (QA should-fix,
    runner.py:158). ``run.lock`` is now only a note of who holds the lock, for the error
    message. The OS releases the lock when the process exits, however it exits (SIGKILL
    included), because closing the last descriptor releases a flock.

    Re-entrancy is per RunDir object: the CLI locks before reading the manifest, then calls
    run_batch with the same handle, and the inner call is a no-op. Any other handle, in this
    process or another, gets RunLockedError. A RunDir is not thread-safe: two threads sharing
    one handle would both pass the re-entrancy check.
    """
    if run._lock_fd is not None:
        yield
        return
    if not run.path.is_dir():
        raise FileNotFoundError(f"run directory {run.path} does not exist")
    fd = os.open(run.path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise
            raise RunLockedError(
                f"run {run.run_id} is in use by another process ({_lock_holder(run)}). Wait "
                "for it to finish or stop it before resuming; two processes on one run would "
                "each enforce the budget separately."
            ) from e
        info = run.file(LOCK_INFO)
        tmp = run.file(LOCK_INFO + ".tmp")
        tmp.write_text(f"pid {os.getpid()} since {now_rfc3339()}\n", encoding="utf-8")
        tmp.replace(info)
        run._lock_fd = fd
        try:
            yield
        finally:
            run._lock_fd = None
    finally:
        os.close(fd)  # closing the descriptor releases the flock


def _lock_holder(run: RunDir) -> str:
    try:
        return run.file(LOCK_INFO).read_text(encoding="utf-8")[:200].strip() or "unknown"
    except OSError:
        return "unknown"


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


def start_run(
    mode: str, cfg: RunConfig, prompt: Prompt, plan: Plan, *, budget_usd: float, **extra: Any
) -> RunDir:
    """Create the run directory and manifest. ``budget_usd`` (the spend Hari approved) is saved
    so every later step, resume included, enforces it."""
    if not (isinstance(budget_usd, (int, float)) and math.isfinite(budget_usd) and budget_usd > 0):
        raise ValueError(f"budget_usd must be a finite amount above 0, got {budget_usd!r}")
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
            "budget_usd": float(budget_usd),
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


# --- spend tracking ------------------------------------------------------------------------


def request_worst_usd(
    prepared: Prepared,
    cfg: RunConfig,
    *,
    batch: bool,
    exact_input_tokens: int | None = None,
) -> float:
    """Upper bound on one request for this title (see cost.py). With an exact input count
    (count_tokens endpoint), every input token is priced at the cache-write rate, the most
    expensive input rate, with the small EXACT_COUNT_MARGIN."""
    if exact_input_tokens is not None:
        return worst_case_request_usd(
            cfg.model,
            static_tokens=exact_input_tokens,
            variable_tokens=0,
            max_tokens=cfg.max_tokens,
            batch=batch,
            input_margin=EXACT_COUNT_MARGIN,
        )
    static, variable = request_token_split(prepared.params)
    return worst_case_request_usd(
        cfg.model,
        static_tokens=static,
        variable_tokens=variable,
        max_tokens=cfg.max_tokens,
        batch=batch,
    )


def _worst_by_title(
    run: RunDir, prepared: Iterable[Prepared], cfg: RunConfig, *, batch: bool
) -> dict[str, float]:
    """Per-title worst case, using exact input counts saved in the manifest when present."""
    exact = run.read_json("manifest.json").get("exact_input_tokens") or {}
    return {
        p.key: request_worst_usd(p, cfg, batch=batch, exact_input_tokens=exact.get(p.key))
        for p in prepared
    }


@dataclass
class SpendTracker:
    """Measured spend of a run (every logged attempt, priced) plus worst-case reserves for
    failed requests that may have been billed, checked against the run's budget."""

    model: str
    batch: bool
    budget_usd: float
    measured_usd: float = 0.0
    reserved_usd: float = 0.0

    @classmethod
    def from_run(cls, run: RunDir, model: str, *, batch: bool) -> SpendTracker:
        budget = run.read_json("manifest.json").get("budget_usd")
        if budget is None:
            raise ValueError(f"run {run.run_id} has no budget_usd in its manifest")
        tracker = cls(model, batch, float(budget))
        for entry in run.read_jsonl("attempts.jsonl"):
            tracker.add(entry)
        return tracker

    def add(self, entry: dict[str, Any]) -> None:
        self.measured_usd += price_usage(self.model, Usage(**entry["usage"]), batch=self.batch)
        self.reserved_usd += float(entry.get("reserved_usd") or 0.0)

    @property
    def spent_usd(self) -> float:
        return self.measured_usd + self.reserved_usd

    def allows(self, next_worst_usd: float) -> bool:
        return self.spent_usd + next_worst_usd <= self.budget_usd + 1e-9

    def refusal(self, next_worst_usd: float, what: str) -> str:
        return (
            f"stopped: budget: spent ${self.spent_usd:.4f} (measured ${self.measured_usd:.4f} + "
            f"reserved ${self.reserved_usd:.4f}) + worst case ${next_worst_usd:.4f} for {what} "
            f"would exceed budget ${self.budget_usd:.4f}"
        )


def _stop(run: RunDir, cfg: RunConfig, status: str, unfinished: Iterable[str]) -> str:
    """End the run cleanly: manifest status, unfinished titles, cost report."""
    _set_manifest(
        run, status=status, stopped_at=now_rfc3339(), unfinished=sorted(set(unfinished))
    )
    write_cost_report(run, cfg.model)
    return status


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
    tracker: SpendTracker,
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
    tracker.add(entry)


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


@dataclass(frozen=True)
class SyncAttempt:
    response: Response | None
    outcome: dict[str, Any] | Invalid
    latency: float
    transient: bool = False


def _attempt_sync(
    api: AnnotationAPI,
    run: RunDir,
    state: TitleState,
    ctx: RunContext,
    tracker: SpendTracker,
    worst_usd: float,
) -> SyncAttempt:
    """One request. RequestRejectedError and TransientAPIError are logged as attempts (a
    possibly-billed failure reserves ``worst_usd``); FatalAPIError propagates."""
    started = time.monotonic()
    round_no = len(state.attempts) + 1
    try:
        response = api.create(state.prepared.params)
    except (RequestRejectedError, TransientAPIError) as e:
        latency = time.monotonic() - started
        transient = isinstance(e, TransientAPIError)
        billed = transient and e.possibly_billed  # type: ignore[union-attr]
        if transient:
            outcome: dict[str, Any] | Invalid = Invalid(f"API error: {e}", retryable=True)
        else:
            outcome = Invalid(f"request rejected: {e}", retryable=False)
        extra: dict[str, Any] = {"latency_s": round(latency, 3), "api_error": str(e)}
        if billed:
            extra["possibly_billed"] = True
            extra["reserved_usd"] = round(worst_usd, 6)
        _log_attempt(
            run, state, round_no=round_no, response=None, outcome=outcome, tracker=tracker,
            extra=extra,
        )
        return SyncAttempt(None, outcome, latency, transient)
    latency = time.monotonic() - started
    state.usage = state.usage + response.usage
    outcome = interpret(
        response,
        state.prepared.gated,
        ctx,
        usage_so_far=state.usage,
        attempts=round_no,
    )
    _log_attempt(
        run,
        state,
        round_no=round_no,
        response=response,
        outcome=outcome,
        tracker=tracker,
        extra={"latency_s": round(latency, 3)},
    )
    return SyncAttempt(response, outcome, latency)


def _backoff(n: int) -> float:
    return min(TRANSIENT_BACKOFF_SECONDS * 2 ** (n - 1), TRANSIENT_BACKOFF_CAP_SECONDS)


def run_sync(
    api: AnnotationAPI,
    run: RunDir,
    prepared: list[Prepared],
    cfg: RunConfig,
    *,
    sleep: Callable[[float], None] | None = None,
) -> str:
    """Annotate titles one request at a time (full price). Retries invalid output and
    transient API errors, within max_attempts and the run's budget. Holds the run lock.
    Returns the final status ("complete" or "stopped: ...")."""
    with run_lock(run):
        return _run_sync(api, run, prepared, cfg, sleep=sleep or time.sleep)


def _run_sync(
    api: AnnotationAPI,
    run: RunDir,
    prepared: list[Prepared],
    cfg: RunConfig,
    *,
    sleep: Callable[[float], None],
) -> str:
    ctx = _context(run, cfg, batch=False)
    tracker = SpendTracker.from_run(run, cfg.model, batch=False)
    worst_by_title = _worst_by_title(run, prepared, cfg, batch=False)
    consecutive_transient = 0
    for i, p in enumerate(prepared):
        state = TitleState(p)
        worst = worst_by_title[p.key]
        unfinished = [q.key for q in prepared[i:]]
        while not state.finished:
            if not tracker.allows(worst):
                return _stop(run, cfg, tracker.refusal(worst, f"{p.key}"), unfinished)
            try:
                attempt = _attempt_sync(api, run, state, ctx, tracker, worst)
            except FatalAPIError as e:
                return _stop(run, cfg, f"stopped: fatal API error: {e}", unfinished)
            _settle(run, state, ctx, attempt.outcome, cfg.max_attempts)
            if attempt.transient:
                consecutive_transient += 1
                if consecutive_transient >= MAX_CONSECUTIVE_TRANSIENT_ERRORS:
                    left = unfinished if not state.finished else unfinished[1:]
                    return _stop(
                        run,
                        cfg,
                        f"stopped: {consecutive_transient} consecutive transient API errors "
                        f"(last: {attempt.outcome.reason})",  # type: ignore[union-attr]
                        left,
                    )
                if not state.finished:
                    sleep(_backoff(consecutive_transient))
            else:
                consecutive_transient = 0
    _set_manifest(run, status="complete", finished_at=now_rfc3339())
    write_cost_report(run, cfg.model)
    return "complete"


def run_smoke(api: AnnotationAPI, run: RunDir, prepared: Prepared, cfg: RunConfig) -> dict:
    """Exactly one request for one title; no retry. Records latency, usage, outcome. Holds
    the run lock."""
    with run_lock(run):
        return _run_smoke(api, run, prepared, cfg)


def _run_smoke(api: AnnotationAPI, run: RunDir, prepared: Prepared, cfg: RunConfig) -> dict:
    ctx = _context(run, cfg, batch=False)
    tracker = SpendTracker.from_run(run, cfg.model, batch=False)
    state = TitleState(prepared)
    worst = _worst_by_title(run, [prepared], cfg, batch=False)[prepared.key]
    if not tracker.allows(worst):
        status = _stop(run, cfg, tracker.refusal(worst, prepared.key), [prepared.key])
        return {"title_key": prepared.key, "status": status}
    try:
        attempt = _attempt_sync(api, run, state, ctx, tracker, worst)
    except FatalAPIError as e:
        status = _stop(run, cfg, f"stopped: fatal API error: {e}", [prepared.key])
        return {"title_key": prepared.key, "status": status}
    response, outcome = attempt.response, attempt.outcome
    _settle(run, state, ctx, outcome, max_attempts=1)
    usage = response.usage if response else Usage()
    report = {
        "title_key": prepared.key,
        "status": "complete",
        "model": cfg.model,
        "served_model": response.model if response else None,
        "latency_s": round(attempt.latency, 3),
        "stop_reason": response.stop_reason if response else None,
        "request_id": response.request_id if response else None,
        "usage": usage.as_dict(),
        "cost_usd": round(price_usage(cfg.model, usage, batch=False), 5),
        "reserved_usd": round(tracker.reserved_usd, 5),
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
    tracker: SpendTracker,
    logged: set[str],
) -> None:
    """Process one ended batch. ``logged`` holds custom ids already in attempts.jsonl: a round
    whose collection was interrupted is collected again on resume, and those results (and their
    records) must not be written twice."""
    ids: dict[str, str] = round_info["custom_ids"]
    seen: set[str] = set()
    for res in api.batch_results(round_info["batch_id"]):
        key = ids.get(res.custom_id)
        if key is None or res.custom_id in seen:
            log(f"ignoring unexpected batch result {res.custom_id!r}")
            continue
        seen.add(res.custom_id)
        if key not in states:
            # The title was dropped on resume (plot input changed or removed while this round
            # was in flight). The result may have been billed: log its usage, store no record.
            if res.custom_id not in logged:
                _log_discarded(run, key, round_info["round"], res, tracker)
                logged.add(res.custom_id)
            continue
        if res.custom_id in logged or states[key].finished:
            continue  # already collected before an interruption
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
        _log_attempt(
            run,
            state,
            round_no=round_info["round"],
            response=response,
            outcome=outcome,
            tracker=tracker,
            extra={"custom_id": res.custom_id},
        )
        logged.add(res.custom_id)
        _settle(run, state, ctx, outcome, cfg.max_attempts)
    for cid, key in ids.items():
        if key not in states:
            continue  # dropped on resume; no result means nothing to log or bill
        if cid not in seen and cid not in logged and not states[key].finished:
            outcome = Invalid("no result returned for this request", retryable=True)
            _log_attempt(
                run,
                states[key],
                round_no=round_info["round"],
                response=None,
                outcome=outcome,
                tracker=tracker,
                extra={"custom_id": cid},
            )
            logged.add(cid)
            _settle(run, states[key], ctx, outcome, cfg.max_attempts)


DISCARDED_REASON = "source changed since this round was submitted; result discarded"


def _log_discarded(
    run: RunDir, key: str, round_no: int, res: Any, tracker: SpendTracker
) -> None:
    """Log a batch result for a title no longer in the run as a priced attempt, so its cost
    is counted, without interpreting it or writing a record."""
    response = res.response
    previous = sum(1 for e in run.read_jsonl("attempts.jsonl") if e["title_key"] == key)
    entry = {
        "title_key": key,
        "attempt": previous + 1,
        "round": round_no,
        "at": now_rfc3339(),
        "usage": response.usage.as_dict() if response else Usage().as_dict(),
        "stop_reason": response.stop_reason if response else None,
        "request_id": response.request_id if response else None,
        "ok": False,
        "reason": f"{DISCARDED_REASON} (batch result {res.kind})",
        "retryable": True,
        "raw_output_excerpt": None,
        "custom_id": res.custom_id,
        "discarded": True,
    }
    run.append("attempts.jsonl", entry)
    tracker.add(entry)


def _restore_states(run: RunDir, prepared: list[Prepared]) -> tuple[dict[str, TitleState], set]:
    """Rebuild per-title state from the run's own files (for resume), and the custom ids
    already logged."""
    states = {p.key: TitleState(p) for p in prepared}
    logged: set[str] = set()
    for entry in run.read_jsonl("attempts.jsonl"):
        if entry.get("custom_id"):
            logged.add(entry["custom_id"])
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
    return states, logged


def run_batch(
    api: AnnotationAPI,
    run: RunDir,
    prepared: list[Prepared],
    cfg: RunConfig,
    *,
    poll_seconds: float,
    sleep: Callable[[float], None] | None = None,
    log: Log = print,
    max_polls: int | None = None,
) -> str:
    """Submit, poll, collect; resubmit retryable failures as a new batch, up to max_attempts
    rounds. Safe to call again on the same run after an interruption (resume). Before each
    submission, measured spend + the round's worst case must fit the manifest's budget_usd.
    Holds the run lock for its whole duration; run state is read after the lock is held.
    Returns the final status ("complete" or "stopped: ...")."""
    with run_lock(run):
        return _run_batch(
            api,
            run,
            prepared,
            cfg,
            poll_seconds=poll_seconds,
            sleep=sleep or time.sleep,
            log=log,
            max_polls=max_polls,
        )


def _run_batch(
    api: AnnotationAPI,
    run: RunDir,
    prepared: list[Prepared],
    cfg: RunConfig,
    *,
    poll_seconds: float,
    sleep: Callable[[float], None],
    log: Log,
    max_polls: int | None,
) -> str:
    ctx = _context(run, cfg, batch=True)
    tracker = SpendTracker.from_run(run, cfg.model, batch=True)
    states, logged = _restore_states(run, prepared)
    worst = _worst_by_title(run, prepared, cfg, batch=True)
    manifest = run.read_json("manifest.json")
    rounds: list[dict[str, Any]] = manifest["rounds"]

    def unfinished() -> list[str]:
        return [k for k, s in states.items() if not s.finished]

    while True:
        if rounds and not rounds[-1]["collected"]:
            current = rounds[-1]
            if current["batch_id"] is None:
                raise RuntimeError(
                    f"round {current['round']} may or may not have been submitted (the run "
                    "stopped during submission). Check the batches in the Anthropic Console "
                    "before resuming, so nothing is paid for twice."
                )
            try:
                wait_for_batch(
                    api,
                    current["batch_id"],
                    poll_seconds=poll_seconds,
                    sleep=sleep,
                    log=log,
                    max_polls=max_polls,
                )
                _collect_round(api, run, current, states, ctx, cfg, log, tracker, logged)
            except FatalAPIError as e:
                return _stop(
                    run, cfg, f"stopped: {e} (batch {current['batch_id']}; resume later)",
                    unfinished(),
                )
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
        round_worst = sum(worst[s.prepared.key] for s in pending)
        if not tracker.allows(round_worst):
            status = tracker.refusal(round_worst, f"round {round_no} ({len(pending)} requests)")
            return _stop(run, cfg, status, unfinished())
        ids = {custom_id(s.prepared.key, round_no): s.prepared.key for s in pending}
        requests = [(cid, states[key].prepared.params) for cid, key in ids.items()]
        rounds.append(
            {
                "round": round_no,
                "batch_id": None,
                "custom_ids": ids,
                "collected": False,
                "worst_usd": round(round_worst, 6),
            }
        )
        _set_manifest(run, rounds=rounds, status=f"round {round_no} submitting")
        try:
            batch_id = api.submit_batch(requests)
        except FatalAPIError as e:
            return _stop(
                run,
                cfg,
                f"stopped: {e}. Round {round_no} may or may not exist: check the Anthropic "
                "Console before resuming",
                unfinished(),
            )
        rounds[-1]["batch_id"] = batch_id
        _set_manifest(run, rounds=rounds, status=f"round {round_no} submitted")
        log(f"round {round_no}: submitted {len(requests)} requests as batch {batch_id}")
    _set_manifest(run, status="complete", finished_at=now_rfc3339())
    write_cost_report(run, cfg.model)
    return "complete"


# --- cost report ---------------------------------------------------------------------------


def write_cost_report(run: RunDir, model: str) -> dict[str, Any]:
    """Measured usage per title (all attempts, failures included), priced offline."""
    manifest = run.read_json("manifest.json")
    batch = manifest["mode"] == "batch"
    per_title: dict[str, Usage] = {}
    reserved = 0.0
    for entry in run.read_jsonl("attempts.jsonl"):
        key = entry["title_key"]
        per_title[key] = per_title.get(key, Usage()) + Usage(**entry["usage"])
        reserved += float(entry.get("reserved_usd") or 0.0)
    total = sum(per_title.values(), Usage())
    annotated = len(run.read_jsonl("annotations.jsonl"))
    total_usd = price_usage(model, total, batch=batch)
    # Rounds submitted (or possibly submitted) but not collected: their results are not in
    # attempts.jsonl yet, so count each at the worst case saved when it was submitted.
    in_flight = sum(
        float(r.get("worst_usd") or 0.0)
        for r in manifest.get("rounds", [])
        if not r.get("collected")
    )
    report = {
        "run_id": run.run_id,
        "model": model,
        "batch": batch,
        "titles_attempted": len(per_title),
        "records_stored": annotated,
        "failures": len(run.read_jsonl("failures.jsonl")),
        "requests": len(run.read_jsonl("attempts.jsonl")),
        "usage_total": total.as_dict(),
        "status": manifest.get("status"),
        "budget_usd": manifest.get("budget_usd"),
        "usd_total": round(total_usd, 5),
        "usd_reserved_possibly_billed": round(reserved, 5),
        "in_flight_worst_usd": round(in_flight, 5),
        "usd_total_with_reserve": round(total_usd + reserved + in_flight, 5),
        "usd_per_title_attempted": round(total_usd / len(per_title), 5) if per_title else None,
        "usd_per_record_stored": round(total_usd / annotated, 5) if annotated else None,
        "per_title_usd": {
            k: round(price_usage(model, u, batch=batch), 6) for k, u in sorted(per_title.items())
        },
        "pricing": "measured tokens x the UNCONFIRMED price table in annotate/config.py; "
        "usd_total_with_reserve adds reserves for possibly-billed failures and the worst case "
        "of submitted rounds not yet collected (in_flight_worst_usd)",
    }
    run.write_json("cost.json", report)
    return report
