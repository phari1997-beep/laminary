"""QA round 3: the run lock (BL1), resume after a plot changed mid-round (SF1), resume of a
non-batch run (SF2), token ratios and exact counts in the worst case (SF3), in-flight rounds in
cost.json, unknown SDK errors, and the resume/CLI nits. Regression tests for QA's probe3
scenarios S1-S3. Fakes only: no network, no key."""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from annotate_support import (
    STORY,
    FakeAPI,
    ingest_plot,
    invalid_output,
    response,
    write_plots,
)

from laminary_pipeline.annotate import __main__ as cli_mod
from laminary_pipeline.annotate import runner as runner_mod
from laminary_pipeline.annotate.__main__ import main
from laminary_pipeline.annotate.client import (
    BatchResult,
    FatalAPIError,
    TransientAPIError,
    Usage,
    classify_sdk_error,
)
from laminary_pipeline.annotate.config import DEFAULT_PROMPT_VERSION, MODELS
from laminary_pipeline.annotate.cost import (
    EXACT_COUNT_MARGIN,
    SCHEMA_CHARS_PER_TOKEN,
    price_usage,
    schema_tokens,
    worst_case_request_usd,
)
from laminary_pipeline.annotate.inputs import GateError, load_plots
from laminary_pipeline.annotate.prompt import load_prompt, wikipedia_article_title
from laminary_pipeline.annotate.runner import (
    DISCARDED_REASON,
    RunConfig,
    RunDir,
    RunLockedError,
    plan_run,
    request_worst_usd,
    run_batch,
    run_lock,
    run_smoke,
    run_sync,
    start_run,
)

MODEL = "claude-opus-5-5"
REAL = Usage(3000, 16000, 0, 9000)  # a plausible worst-ish batch response


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_mod.time, "sleep", lambda s: None)


@pytest.fixture
def env(tmp_path: Path):
    plots = tmp_path / "plots"
    write_plots(plots, [ingest_plot("Q1", 11), ingest_plot("Q2", 12)])
    cfg = RunConfig(model=MODEL, out_root=tmp_path / "annotations")
    return plots, cfg


def cli(args: list[str], api=None) -> tuple[int, str]:
    lines: list[str] = []

    def factory():
        if api is None:
            raise AssertionError("no API client expected")
        return api

    code = main(args, api_factory=factory, out=lines.append)
    return code, "\n".join(lines)


def all_invalid(usage: Usage = REAL):
    def script(round_no, requests):
        return [
            BatchResult(cid, "succeeded", response(invalid_output(), usage=usage))
            for cid, _ in requests
        ]

    return script


def prepare(plots: Path, cfg: RunConfig, mode: str, budget: float, **extra):
    prompt = load_prompt(DEFAULT_PROMPT_VERSION)
    plan = plan_run(load_plots(plots).plots, cfg, prompt)
    return plan, start_run(mode, cfg, prompt, plan, budget_usd=budget, **extra)


def measured_spend(run: RunDir, *, batch: bool = True) -> float:
    return sum(
        price_usage(MODEL, Usage(**e["usage"]), batch=batch)
        for e in run.read_jsonl("attempts.jsonl")
    )


# --- BL1: one process per run --------------------------------------------------------------


def test_second_run_batch_on_a_locked_run_refuses(env) -> None:
    """probe3 S2: B starts while A is polling round 1. B must refuse; A alone spends."""
    plots, cfg = env
    plan, _ = prepare(plots, cfg, "batch", budget=100)
    worst = sum(request_worst_usd(p, cfg, batch=True) for p in plan.todo)
    budget = 3 * worst
    plan, run = prepare(plots, cfg, "batch", budget=budget)
    api = FakeAPI(batch_script=all_invalid(), polls_before_end=1)
    refusals: list[str] = []

    def sleep_a(_s: float) -> None:
        if refusals:
            return
        with pytest.raises(RunLockedError) as e:
            run_batch(api, RunDir(cfg.out_root, run.run_id), plan.todo, cfg, poll_seconds=0,
                      sleep=lambda s: None, log=lambda m: None)
        refusals.append(str(e.value))

    run_batch(api, run, plan.todo, cfg, poll_seconds=0, sleep=sleep_a, log=lambda m: None)
    assert refusals and "in use by another process" in refusals[0]
    assert f"pid {os.getpid()}" in refusals[0]
    att = run.read_jsonl("attempts.jsonl")
    # every attempt logged once, by A only
    assert len({e["custom_id"] for e in att}) == len(att)
    assert measured_spend(run) <= budget + 1e-9
    rounds = run.read_json("manifest.json")["rounds"]
    assert [r["batch_id"] for r in rounds] == [f"msgbatch_{i + 1}" for i in range(len(rounds))]
    assert len(api.submitted) == len(rounds)


def test_sync_and_smoke_hold_the_lock(env) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "single", budget=100)
    with run_lock(RunDir(cfg.out_root, run.run_id)):
        with pytest.raises(RunLockedError):
            run_sync(FakeAPI([response()]), run, plan.todo[:1], cfg)
        with pytest.raises(RunLockedError):
            run_smoke(FakeAPI([response()]), run, plan.todo[0], cfg)
    assert run.read_jsonl("attempts.jsonl") == []
    # released: the same calls now run
    assert run_sync(FakeAPI([response()]), run, plan.todo[:1], cfg) == "complete"


def test_lock_is_released_when_a_run_raises(env) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "batch", budget=100)
    api = FakeAPI(batch_script=all_invalid(Usage(10, 10, 0, 0)), polls_before_end=5)
    with pytest.raises(TimeoutError):
        run_batch(api, run, plan.todo, cfg, poll_seconds=0, log=lambda m: None, max_polls=1)
    with run_lock(RunDir(cfg.out_root, run.run_id)):
        pass  # acquirable again


def hold_lock_in_subprocess(run: RunDir) -> subprocess.Popen:
    """Another pipeline process holding ``run``'s lock until its stdin gets a line."""
    holder = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        from laminary_pipeline.annotate.runner import RunDir, run_lock
        with run_lock(RunDir(Path({str(run.path.parent)!r}), {run.run_id!r})):
            print("locked", flush=True)
            sys.stdin.readline()
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", holder], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout.readline().strip() == "locked"
    return proc


def test_cli_resume_refuses_while_another_process_holds_the_lock(env, tmp_path) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "batch", budget=100)
    proc = hold_lock_in_subprocess(run)
    try:
        before = run.read_json("manifest.json")
        code, text = cli(
            ["batch", "--resume", run.run_id, "--plots-dir", str(plots), "--out-dir",
             str(cfg.out_root), "--budget-usd", "200", "--allow-budget-increase"],
            api=FakeAPI(batch_script=all_invalid()),
        )
        assert code == 2 and "in use by another process" in text
        assert f"pid {proc.pid}" in text
        assert run.read_json("manifest.json") == before  # nothing written, budget unchanged
    finally:
        proc.communicate("\n", timeout=10)
    # the holder exited, so the OS released the lock
    with run_lock(RunDir(cfg.out_root, run.run_id)):
        pass


def test_deleting_run_lock_by_hand_does_not_bypass_the_lock(env) -> None:
    """QA should-fix (runner.py:158): the lock is on the run directory, not on run.lock."""
    plots, cfg = env
    plan, run = prepare(plots, cfg, "batch", budget=100)
    with run_lock(RunDir(cfg.out_root, run.run_id)):
        run.file("run.lock").unlink()
        with pytest.raises(RunLockedError, match="unknown"):  # holder note gone, lock held
            with run_lock(RunDir(cfg.out_root, run.run_id)):
                pass
    proc = hold_lock_in_subprocess(run)
    try:
        run.file("run.lock").unlink()
        with pytest.raises(RunLockedError):
            run_sync(FakeAPI([response()]), RunDir(cfg.out_root, run.run_id), plan.todo[:1], cfg)
    finally:
        proc.communicate("\n", timeout=10)
    assert run.read_jsonl("attempts.jsonl") == []


def test_sigkill_releases_the_lock(env) -> None:
    plots, cfg = env
    _, run = prepare(plots, cfg, "batch", budget=100)
    proc = hold_lock_in_subprocess(run)
    try:
        with pytest.raises(RunLockedError, match=f"pid {proc.pid}"):
            with run_lock(RunDir(cfg.out_root, run.run_id)):
                pass
    finally:
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
    assert proc.returncode == -signal.SIGKILL
    with run_lock(RunDir(cfg.out_root, run.run_id)):  # the OS released it
        pass


def test_lock_reentrancy_is_per_handle(env) -> None:
    plots, cfg = env
    _, run = prepare(plots, cfg, "batch", budget=100)
    with run_lock(run):
        with run_lock(run):  # same handle: no-op
            pass
        assert run._lock_fd is not None  # the inner exit didn't release the outer lock
        with pytest.raises(RunLockedError):
            with run_lock(RunDir(cfg.out_root, run.run_id)):
                pass


# --- SF1 / probe3 S1: plot changed while a round was in flight -----------------------------


def test_resume_after_plot_changed_mid_round_logs_the_billed_result(env) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "batch", budget=5)
    api = FakeAPI(batch_script=all_invalid(), polls_before_end=5)
    with pytest.raises(TimeoutError):
        run_batch(api, run, plan.todo, cfg, poll_seconds=0, log=lambda m: None, max_polls=1)
    write_plots(plots, [ingest_plot("Q2", 12, text=STORY.replace("the", "a", 3))])
    api.polls_before_end = 0
    code, text = cli(
        ["batch", "--resume", run.run_id, "--plots-dir", str(plots), "--out-dir",
         str(cfg.out_root), "--poll-seconds", "1"],
        api=api,
    )
    assert code in (0, 3), text
    att = run.read_jsonl("attempts.jsonl")
    discarded = [e for e in att if e["title_key"] == "movie:12"]
    assert len(discarded) == 1  # round 1's billed result, never resubmitted
    assert discarded[0]["reason"].startswith(DISCARDED_REASON)
    assert discarded[0]["usage"] == REAL.as_dict() and discarded[0]["discarded"]
    # no record for the changed title; one failure saying why
    keys = [r["title"]["tmdb_id"] for r in run.read_jsonl("annotations.jsonl")]
    assert 12 not in keys
    fails = [f for f in run.read_jsonl("failures.jsonl") if f["title_key"] == "movie:12"]
    assert len(fails) == 1 and "source text changed" in fails[0]["reason"]
    # later rounds only carried movie:11
    for reqs in api.submitted[1:]:
        assert all(cid.startswith("movie-11-") for cid, _ in reqs)
    cost = run.read_json("cost.json")
    assert cost["per_title_usd"]["movie:12"] == pytest.approx(
        price_usage(MODEL, REAL, batch=True), abs=1e-6
    )


# --- SF2 / probe3 S3: batch --resume only resumes batch runs -------------------------------


@pytest.mark.parametrize("mode", ["single", "smoke"])
def test_batch_resume_refuses_a_non_batch_run(env, mode) -> None:
    plots, cfg = env
    _, run = prepare(plots, cfg, mode, budget=1.2)
    api = FakeAPI(batch_script=all_invalid())
    with pytest.raises(SystemExit, match="only batch runs can be resumed"):
        cli(["batch", "--resume", run.run_id, "--plots-dir", str(plots), "--out-dir",
             str(cfg.out_root)], api=api)
    assert api.submitted == []


# --- SF3: token ratios and exact counts ----------------------------------------------------


def test_schema_json_uses_the_denser_ratio() -> None:
    from laminary_pipeline.model_output import model_output_schema

    chars = len(json.dumps(model_output_schema(), separators=(",", ":")))
    assert SCHEMA_CHARS_PER_TOKEN <= 2.0
    assert schema_tokens() == math.ceil(chars / SCHEMA_CHARS_PER_TOKEN)


def test_exact_counts_price_all_input_at_the_cache_write_rate(env) -> None:
    plots, cfg = env
    plan, _ = prepare(plots, cfg, "batch", budget=100)
    p = plan.todo[0]
    got = request_worst_usd(p, cfg, batch=True, exact_input_tokens=20_000)
    spec = MODELS[MODEL]
    assert EXACT_COUNT_MARGIN == 1.05
    want = (21_000 * spec.cache_write_per_mtok + cfg.max_tokens * spec.output_per_mtok) / 1e6
    assert got == pytest.approx(want * 0.5)
    assert got == pytest.approx(
        worst_case_request_usd(MODEL, static_tokens=20_000, variable_tokens=0,
                               max_tokens=cfg.max_tokens, batch=True,
                               input_margin=EXACT_COUNT_MARGIN)
    )


def test_count_tokens_counts_are_saved_and_used_by_the_runner(env, tmp_path) -> None:
    plots, cfg = env
    titles = tmp_path / "titles.txt"
    titles.write_text("movie:11\nmovie:12\n")
    api = FakeAPI(batch_script=all_invalid(Usage(10, 10, 0, 0)), polls_before_end=0)
    code, _ = cli(
        ["batch", "--model", MODEL, "--titles-file", str(titles), "--plots-dir", str(plots),
         "--out-dir", str(cfg.out_root), "--budget-usd", "50", "--count-tokens",
         "--max-attempts", "1"],
        api=api,
    )
    assert code == 0  # complete: both titles end as failures (out of attempts)
    run = RunDir(cfg.out_root, next(cfg.out_root.iterdir()).name)
    manifest = run.read_json("manifest.json")
    assert manifest["exact_input_tokens"] == {"movie:11": 9999, "movie:12": 9999}
    plan, _ = prepare(plots, RunConfig(model=MODEL, out_root=tmp_path / "x"), "batch", 100)
    per = request_worst_usd(plan.todo[0], cfg, batch=True, exact_input_tokens=9999)
    assert manifest["rounds"][0]["worst_usd"] == pytest.approx(2 * per, abs=1e-6)


# --- minor: in-flight rounds in cost.json; unknown SDK errors -------------------------------


class StatusFails(FakeAPI):
    def batch_status(self, batch_id):
        raise FatalAPIError("batch status failed: HTTP 401")


def test_cost_report_counts_the_uncollected_round(env) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "batch", budget=100)
    status = run_batch(StatusFails(batch_script=all_invalid()), run, plan.todo, cfg,
                       poll_seconds=0, log=lambda m: None)
    assert status.startswith("stopped:")
    cost = run.read_json("cost.json")
    worst = sum(request_worst_usd(p, cfg, batch=True) for p in plan.todo)
    assert cost["in_flight_worst_usd"] == pytest.approx(worst, abs=1e-5)
    assert cost["usd_total"] == 0
    assert cost["usd_total_with_reserve"] == pytest.approx(worst, abs=1e-5)


def test_unrecognized_sdk_errors_reserve_the_worst_case() -> None:
    anthropic = pytest.importorskip("anthropic")
    import httpx2

    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx2.Response(200, request=req, json={"bad": "shape"})
    e = classify_sdk_error(anthropic.APIResponseValidationError(resp, None))
    assert isinstance(e, TransientAPIError) and e.possibly_billed


# --- nits ----------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "0.5", "-1", "nan", "inf"])
def test_poll_seconds_must_be_finite_and_at_least_one(value) -> None:
    with pytest.raises(SystemExit):
        cli(["batch", "--resume", "x", "--poll-seconds", value])


def _stopped_batch_run(plots: Path, cfg: RunConfig) -> RunDir:
    plan, run = prepare(plots, cfg, "batch", budget=100)
    api = FakeAPI(batch_script=all_invalid(Usage(10, 10, 0, 0)), polls_before_end=5)
    with pytest.raises(TimeoutError):
        run_batch(api, run, plan.todo, cfg, poll_seconds=0, log=lambda m: None, max_polls=1)
    return run


def test_resume_persists_a_raised_budget_only_after_all_checks(env, tmp_path) -> None:
    plots, cfg = env
    run = _stopped_batch_run(plots, cfg)
    manifest = run.read_json("manifest.json")
    manifest["prompt_sha256"] = "0" * 64
    run.write_json("manifest.json", manifest)
    with pytest.raises(SystemExit, match="prompt text differs"):
        cli(["batch", "--resume", run.run_id, "--plots-dir", str(plots), "--out-dir",
             str(cfg.out_root), "--budget-usd", "500", "--allow-budget-increase"],
            api=FakeAPI())
    after = run.read_json("manifest.json")
    assert after["budget_usd"] == 100 and "budget_history" not in after


def test_resume_warns_about_ignored_settings(env) -> None:
    plots, cfg = env
    run = _stopped_batch_run(plots, cfg)
    lines: list[str] = []
    manifest = run.read_json("manifest.json")
    args = cli_mod._parser().parse_args(
        ["batch", "--resume", run.run_id, "--effort", "low", "--max-tokens", "1000",
         "--model", manifest["model"]]
    )
    cli_mod._warn_ignored_flags(args, manifest, lines.append)
    assert any("--effort low ignored" in x for x in lines)
    assert any("--max-tokens 1000 ignored" in x for x in lines)
    assert not any("--model" in x for x in lines)  # same as the run's: no warning
    args = cli_mod._parser().parse_args(["batch", "--resume", run.run_id])
    lines.clear()
    cli_mod._warn_ignored_flags(args, manifest, lines.append)
    assert lines == []


@pytest.mark.parametrize(
    "ref",
    [
        "https://en.wikipedia.org/wiki/Talk:The_Matrix",
        "https://en.wikipedia.org/wiki/Special:Random",
        "https://en.wikipedia.org/wiki/User:Someone",
        "https://en.wikipedia.org/wiki/Wikipedia:About",
        "https://en.wikipedia.org/wiki/Category:1999_films",
        "https://en.wikipedia.org/wiki/Template_talk:Film",
        "https://en.wikipedia.org/wiki/WP:PLOT",
        "https://en.wikipedia.org/wiki/file:Poster.jpg",
        # QA should-fix: spaces collapsed, one leading ':' dropped (MediaWiki normalization)
        "https://en.wikipedia.org/wiki/Template__talk:Film",
        "https://en.wikipedia.org/wiki/_Talk_:The_Matrix",
        "https://en.wikipedia.org/wiki/:Category:1999_films",
        "https://en.wikipedia.org/wiki/:_Talk:X",
        # interwiki and language prefixes
        "https://en.wikipedia.org/wiki/fr:Matrix",
        "https://en.wikipedia.org/wiki/wikt:matrix",
        "https://en.wikipedia.org/wiki/commons:File:X.jpg",
        "https://en.wikipedia.org/wiki/m:Main_Page",
        "https://en.wikipedia.org/wiki/zh-yue:X",
        "https://en.wikipedia.org/wiki/EN:The_Matrix",
        "https://en.wikipedia.org/wiki/:de:Matrix",
        # pseudo-namespaces
        "https://en.wikipedia.org/wiki/MOS:PLOT",
        "https://en.wikipedia.org/wiki/CAT:X",
        "https://en.wikipedia.org/wiki/H:Links",
    ],
)
def test_non_article_namespaces_are_refused(ref) -> None:
    with pytest.raises(GateError, match="not articles"):
        wikipedia_article_title(ref)


@pytest.mark.parametrize(
    "ref,title",
    [
        ("https://en.wikipedia.org/wiki/Alien:_Covenant", "Alien: Covenant"),
        ("https://en.wikipedia.org/wiki/Star_Trek:_The_Motion_Picture",
         "Star Trek: The Motion Picture"),
        ("https://en.wikipedia.org/wiki/2001:_A_Space_Odyssey_(film)",
         "2001: A Space Odyssey (film)"),
        ("https://en.wikipedia.org/wiki/1:_Nenokkadine", "1: Nenokkadine"),
        ("https://en.wikipedia.org/wiki/Xena:_Warrior_Princess", "Xena: Warrior Princess"),
        ("https://en.wikipedia.org/wiki/Mad_Max:_Fury_Road", "Mad Max: Fury Road"),
    ],
)
def test_titles_with_colons_are_still_articles(ref, title) -> None:
    assert wikipedia_article_title(ref) == title
