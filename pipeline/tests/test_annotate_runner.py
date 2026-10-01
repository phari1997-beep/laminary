"""Annotation runs with a fake API: records, retries, failures, idempotency, the batch
lifecycle, resume, smoke, dry run and the spend guards. No network, no key."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from annotate_support import (
    FakeAPI,
    ingest_plot,
    invalid_output,
    response,
    valid_output,
    write_plots,
)

from laminary_pipeline.annotate.__main__ import main
from laminary_pipeline.annotate.client import BatchResult, RequestRejectedError, Usage
from laminary_pipeline.annotate.config import DEFAULT_PROMPT_VERSION
from laminary_pipeline.annotate.inputs import load_plots
from laminary_pipeline.annotate.prompt import load_prompt
from laminary_pipeline.annotate.runner import (
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
from laminary_pipeline.annotation import validate_record

MODEL = "claude-opus-5-5"


def jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def env(tmp_path: Path):
    plots = tmp_path / "plots"
    write_plots(plots, [ingest_plot("Q1", 11), ingest_plot("Q2", 12), ingest_plot("Q3", 13)])
    cfg = RunConfig(model=MODEL, out_root=tmp_path / "annotations")
    return plots, cfg


def prepare(
    plots: Path, cfg: RunConfig, mode: str, limit: int | None = None, budget: float = 100.0
):
    prompt = load_prompt(DEFAULT_PROMPT_VERSION)
    plan = plan_run(load_plots(plots).plots, cfg, prompt, limit=limit)
    return plan, start_run(mode, cfg, prompt, plan, budget_usd=budget)


# --- synchronous ---------------------------------------------------------------------------


def test_single_success_writes_a_valid_record_with_provenance(env) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "single", limit=1)
    api = FakeAPI([response(usage=Usage(1000, 2000, 5000, 7))])
    run_sync(api, run, plan.todo, cfg)
    [rec] = jsonl(run.file("annotations.jsonl"))
    assert validate_record(rec) == []
    ann = rec["provenance"]["annotator"]
    assert ann == {
        "model_version": MODEL, "prompt_version": DEFAULT_PROMPT_VERSION, "run_id": run.run_id
    }
    assert DEFAULT_PROMPT_VERSION == "annotate-1.1.0"
    assert rec["provenance"]["usage"] == {
        "input_tokens": 1000,
        "output_tokens": 2000,
        "cache_read_input_tokens": 5000,
        "batch": False,
    }
    assert "cache_creation_input_tokens=7" in rec["provenance"]["notes"]
    assert rec["provenance"]["sources"] == [plan.todo[0].gated.plot.sources[0].meta]
    # arc label derived by the pipeline, not taken from the model
    assert rec["layers"]["structural_skeleton"]["emotional_arc"]["method"] == "derived"
    cost = run.read_json("cost.json")
    assert cost["records_stored"] == 1 and cost["usd_total"] > 0
    assert run.read_json("manifest.json")["status"] == "complete"


def test_invalid_output_is_retried_then_stored_with_summed_usage(env) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "single", limit=1)
    api = FakeAPI(
        [response(invalid_output(), usage=Usage(10, 20, 0, 0)), response(usage=Usage(1, 2, 3, 0))]
    )
    run_sync(api, run, plan.todo, cfg)
    [rec] = jsonl(run.file("annotations.jsonl"))
    assert rec["provenance"]["usage"]["input_tokens"] == 11
    assert rec["provenance"]["usage"]["output_tokens"] == 22
    assert "attempts=2" in rec["provenance"]["notes"]
    attempts = jsonl(run.file("attempts.jsonl"))
    assert [a["ok"] for a in attempts] == [False, True]
    assert "primary plot" in attempts[0]["reason"]


@pytest.mark.parametrize(
    "bad, reason",
    [
        (lambda: response(invalid_output()), "primary plot"),
        (lambda: response("{not json"), "not JSON"),
        (lambda: response({"outcome": "annotated"}), "output schema"),
    ],
)
def test_schema_invalid_output_retries_then_records_failure(env, bad, reason) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "single", limit=1)
    api = FakeAPI([bad() for _ in range(cfg.max_attempts)])
    run_sync(api, run, plan.todo, cfg)
    assert len(api.created) == cfg.max_attempts == 3
    assert jsonl(run.file("annotations.jsonl")) == []
    [fail] = jsonl(run.file("failures.jsonl"))
    assert reason in fail["reason"]
    assert fail["retryable"] is True
    assert len(fail["attempts"]) == 3
    assert fail["title_key"] == "movie:11"
    assert fail["raw_output_excerpt"]
    assert run.read_json("cost.json")["failures"] == 1


@pytest.mark.parametrize(
    "item, reason",
    [
        (response(stop_reason="refusal"), "refusal"),
        (response(stop_reason="max_tokens"), "max_tokens"),
        (response(model="claude-opus-5"), "answered by"),
        (RequestRejectedError("HTTP 400: Schema is too complex"), "too complex"),
    ],
)
def test_non_retryable_outcomes_fail_after_one_attempt(env, item, reason) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "single", limit=1)
    api = FakeAPI([item])
    run_sync(api, run, plan.todo, cfg)
    [fail] = jsonl(run.file("failures.jsonl"))
    assert reason in fail["reason"] and fail["retryable"] is False
    assert len(api.created) == 1


def test_abstention_is_a_stored_record(env) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "single", limit=1)
    out = {
        "outcome": "abstained",
        "abstain_reason": "summary_too_thin",
        "layers": None,
        "beat_tags": None,
    }
    run_sync(FakeAPI([response(out)]), run, plan.todo, cfg)
    [rec] = jsonl(run.file("annotations.jsonl"))
    assert rec["outcome"] == "abstained" and validate_record(rec) == []


# --- idempotency ---------------------------------------------------------------------------


def test_done_titles_are_skipped_only_for_same_model_prompt_and_source(env, tmp_path) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "single", limit=1)
    run_sync(FakeAPI([response()]), run, plan.todo, cfg)
    prompt = load_prompt(DEFAULT_PROMPT_VERSION)
    again = plan_run(load_plots(plots).plots, cfg, prompt)
    assert again.already_done == ["movie:11"]
    assert [p.key for p in again.todo] == ["movie:12", "movie:13"]
    other_model = RunConfig(model="claude-sonnet-5-5", out_root=cfg.out_root)
    assert plan_run(load_plots(plots).plots, other_model, prompt).already_done == []
    # a changed source (new revision text) is annotated again
    new_text = " ".join(ingest_plot()["text"].split()[:160])
    write_plots(plots, [ingest_plot("Q1", 11, text=new_text)])
    assert plan_run(load_plots(plots).plots, cfg, prompt).already_done == []


def test_failures_are_not_treated_as_done(env) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "single", limit=1)
    run_sync(FakeAPI([response(stop_reason="refusal")]), run, plan.todo, cfg)
    assert existing_done_keys(cfg.out_root) == set()


# --- batch ---------------------------------------------------------------------------------


def scripted(round_no: int, requests):
    """Round 1: Q1 valid, Q2 invalid (retry), Q3 expired (retry). Round 2: both valid."""
    ids = [cid for cid, _ in requests]
    if round_no == 1:
        return [
            BatchResult(ids[0], "succeeded", response(usage=Usage(100, 200, 0, 50))),
            BatchResult(ids[1], "succeeded", response(invalid_output())),
            BatchResult(ids[2], "expired"),
        ]
    return [BatchResult(cid, "succeeded", response()) for cid in ids]


def test_batch_lifecycle_with_retry_round(env) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "batch")
    api = FakeAPI(batch_script=scripted, polls_before_end=2)
    sleeps: list[float] = []
    run_batch(api, run, plan.todo, cfg, poll_seconds=7, sleep=sleeps.append, log=lambda s: None)
    assert [len(r) for r in api.submitted] == [3, 2]
    assert [cid for cid, _ in api.submitted[1]] == ["movie-12-r2", "movie-13-r2"]
    assert sleeps == [7, 7, 7, 7]
    records = jsonl(run.file("annotations.jsonl"))
    assert sorted(r["title"]["tmdb_id"] for r in records) == [11, 12, 13]
    assert all(r["provenance"]["usage"]["batch"] is True for r in records)
    manifest = run.read_json("manifest.json")
    assert [r["batch_id"] for r in manifest["rounds"]] == ["msgbatch_1", "msgbatch_2"]
    assert all(r["collected"] for r in manifest["rounds"]) and manifest["status"] == "complete"
    assert run.read_json("cost.json")["batch"] is True


def test_batch_invalid_request_error_fails_without_retry_and_others_exhaust(env) -> None:
    plots, cfg = env
    cfg.max_attempts = 2

    def script(round_no, requests):
        out = []
        for cid, _ in requests:
            if cid.startswith("movie-11"):
                out.append(
                    BatchResult(
                        cid,
                        "errored",
                        error_type="invalid_request_error",
                        error_message="bad schema",
                    )
                )
            elif cid.startswith("movie-12"):
                out.append(BatchResult(cid, "succeeded", response("{oops")))
            # movie-13: no result at all
        return out

    plan, run = prepare(plots, cfg, "batch")
    api = FakeAPI(batch_script=script, polls_before_end=0)
    run_batch(api, run, plan.todo, cfg, poll_seconds=0, sleep=lambda s: None, log=lambda s: None)
    fails = {f["title_key"]: f for f in jsonl(run.file("failures.jsonl"))}
    assert set(fails) == {"movie:11", "movie:12", "movie:13"}
    assert len(fails["movie:11"]["attempts"]) == 1 and not fails["movie:11"]["retryable"]
    assert len(fails["movie:12"]["attempts"]) == 2 and "not JSON" in fails["movie:12"]["reason"]
    assert "no result" in fails["movie:13"]["reason"]
    assert len(api.submitted) == 2


def test_batch_resume_collects_without_resubmitting(env) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "batch")
    api = FakeAPI(
        batch_script=lambda n, reqs: [BatchResult(c, "succeeded", response()) for c, _ in reqs],
        polls_before_end=5,
    )
    with pytest.raises(TimeoutError):
        run_batch(
            api,
            run,
            plan.todo,
            cfg,
            poll_seconds=0,
            sleep=lambda s: None,
            log=lambda s: None,
            max_polls=2,
        )
    assert len(api.submitted) == 1
    assert run.read_json("manifest.json")["rounds"][0]["batch_id"] == "msgbatch_1"
    run_batch(
        api,
        RunDir(cfg.out_root, run.run_id),
        plan.todo,
        cfg,
        poll_seconds=0,
        sleep=lambda s: None,
        log=lambda s: None,
    )
    assert len(api.submitted) == 1
    assert len(jsonl(run.file("annotations.jsonl"))) == 3


def test_batch_resume_refuses_an_unconfirmed_submission(env) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "batch")
    manifest = run.read_json("manifest.json")
    manifest["rounds"] = [{"round": 1, "batch_id": None, "custom_ids": {}, "collected": False}]
    run.write_json("manifest.json", manifest)
    with pytest.raises(RuntimeError, match="paid for twice"):
        run_batch(FakeAPI(), run, plan.todo, cfg, poll_seconds=0, log=lambda s: None)


def test_custom_id_format() -> None:
    assert custom_id("tv_series:1396", 3) == "tv_series-1396-r3"


# --- smoke ---------------------------------------------------------------------------------


def test_smoke_sends_exactly_one_request_even_when_invalid(env) -> None:
    plots, cfg = env
    cfg.max_attempts = 1
    plan, run = prepare(plots, cfg, "smoke", limit=1)
    api = FakeAPI([response(invalid_output(), usage=Usage(900, 1500, 0, 5400))])
    report = run_smoke(api, run, plan.todo[0], cfg)
    assert len(api.created) == 1
    assert report["schema_accepted"] is True and report["valid_record"] is False
    assert report["usage"]["cache_creation_input_tokens"] == 5400
    assert report["cost_usd"] > 0 and "latency_s" in report
    assert run.read_json("smoke.json") == report
    assert len(jsonl(run.file("failures.jsonl"))) == 1


def test_smoke_reports_a_rejected_schema(env) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "smoke", limit=1)
    report = run_smoke(
        FakeAPI([RequestRejectedError("HTTP 400: Schema is too complex")]), run, plan.todo[0], cfg
    )
    assert report["schema_accepted"] is False and "too complex" in report["reason"]


# --- CLI -----------------------------------------------------------------------------------


def no_api():
    raise AssertionError("a dry run must not create an API client")


def cli(args: list[str], api_factory=no_api) -> tuple[int, str]:
    lines: list[str] = []
    code = main(args, api_factory=api_factory, out=lines.append)
    return code, "\n".join(lines)


def test_single_dry_run_prints_exact_request_and_writes_nothing(env, monkeypatch) -> None:
    plots, cfg = env
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    code, text = cli(
        [
            "single",
            "--title",
            "movie:11",
            "--model",
            MODEL,
            "--dry-run",
            "--plots-dir",
            str(plots),
            "--out-dir",
            str(cfg.out_root),
        ]
    )
    assert code == 0
    assert "DRY RUN" in text and "Titles to annotate: 1" in text and "Cost estimate: $" in text
    request = json.loads(text[text.index("Exact request:") + len("Exact request:") :])
    assert request["model"] == MODEL and request["messages"][0]["role"] == "user"
    assert not cfg.out_root.exists()


def test_batch_dry_run_counts_and_dumps_requests(env, tmp_path) -> None:
    plots, cfg = env
    dump = tmp_path / "requests.jsonl"
    code, text = cli(
        [
            "batch",
            "--limit",
            "2",
            "--model",
            MODEL,
            "--dry-run",
            "--plots-dir",
            str(plots),
            "--out-dir",
            str(cfg.out_root),
            "--dump-requests",
            str(dump),
        ]
    )
    assert code == 0 and "Titles to annotate: 2" in text
    lines = jsonl(dump)
    assert [x["custom_id"] for x in lines] == ["movie-11-r1", "movie-12-r1"]
    assert not cfg.out_root.exists()


def test_smoke_dry_run(env) -> None:
    plots, _ = env
    code, text = cli(
        [
            "smoke",
            "--title",
            "Q2",
            "--model",
            "claude-sonnet-5-5",
            "--allow-non-phase1-model",
            "--dry-run",
            "--plots-dir",
            str(plots),
        ]
    )
    assert code == 0 and "Mode: smoke" in text and "max attempts 1" in text


def test_live_runs_need_a_budget_that_covers_the_estimate(env) -> None:
    plots, cfg = env
    base = [
        "single",
        "--title",
        "movie:11",
        "--model",
        MODEL,
        "--plots-dir",
        str(plots),
        "--out-dir",
        str(cfg.out_root),
    ]
    api = FakeAPI([response()])
    with pytest.raises(SystemExit, match="budget-usd"):
        cli(base, api_factory=lambda: api)
    with pytest.raises(SystemExit, match="exceeds"):
        cli([*base, "--budget-usd", "0.001"], api_factory=lambda: api)
    assert api.created == []
    code, _ = cli([*base, "--budget-usd", "5"], api_factory=lambda: api)
    assert code == 0 and len(api.created) == 1


def test_batch_over_pilot_cap_refused(env) -> None:
    plots, _ = env
    with pytest.raises(SystemExit, match="allow-over-pilot"):
        cli(["batch", "--limit", "501", "--model", MODEL, "--dry-run", "--plots-dir", str(plots)])


def test_gate_refusal_reported_by_cli(env, tmp_path) -> None:
    plots, _ = env
    write_plots(plots, [ingest_plot("Q7", 17, kind="tmdb_overview")])
    code, text = cli(
        ["single", "--title", "movie:17", "--model", MODEL, "--dry-run", "--plots-dir", str(plots)]
    )
    assert code == 2 and "Refused by the gate" in text


def test_api_key_never_written_to_run_files(env, monkeypatch) -> None:
    plots, cfg = env
    secret = "sk-ant-TEST-SECRET-DO-NOT-LOG"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    code, text = cli(
        [
            "single",
            "--title",
            "movie:11",
            "--model",
            MODEL,
            "--plots-dir",
            str(plots),
            "--out-dir",
            str(cfg.out_root),
            "--budget-usd",
            "5",
        ],
        api_factory=lambda: FakeAPI([response()]),
    )
    assert code == 0 and secret not in text
    for path in cfg.out_root.rglob("*"):
        if path.is_file():
            assert secret not in path.read_text()


def test_valid_output_fixture_is_valid() -> None:
    assert valid_output()["outcome"] == "annotated"


def test_live_run_without_key_exits_cleanly(env, monkeypatch) -> None:
    from laminary_pipeline.annotate.client import make_api

    plots, cfg = env
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    code, text = cli(
        [
            "smoke",
            "--title",
            "movie:11",
            "--model",
            MODEL,
            "--plots-dir",
            str(plots),
            "--out-dir",
            str(cfg.out_root),
            "--budget-usd",
            "1",
        ],
        api_factory=make_api,
    )
    assert code == 2 and "ANTHROPIC_API_KEY is not set" in text
    assert not cfg.out_root.exists()
