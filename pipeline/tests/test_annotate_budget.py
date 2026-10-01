"""QA round 2: budget enforcement (B1-B3), API error handling (M5), title scoping (M2), resume
dedup (minor 2), the Phase 1 model lock (minor 7), dry-run output (nit) and the SDK adapter's
retry and base_url settings. Fakes only: no network, no key."""

from __future__ import annotations

import json
import math
import urllib.request
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from annotate_support import (
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
    RequestRejectedError,
    SdkAnnotationAPI,
    TransientAPIError,
    Usage,
    classify_sdk_error,
)
from laminary_pipeline.annotate.config import DEFAULT_PROMPT_VERSION, MODELS
from laminary_pipeline.annotate.cost import (
    INPUT_ESTIMATE_MARGIN,
    estimate,
    price_usage,
    projection_table,
    request_token_split,
    worst_case_request_usd,
)
from laminary_pipeline.annotate.inputs import (
    InputFormatError,
    load_plots,
    read_titles_file,
    select_plots,
)
from laminary_pipeline.annotate.prompt import load_prompt
from laminary_pipeline.annotate.runner import (
    RunConfig,
    RunDir,
    plan_run,
    request_worst_usd,
    run_batch,
    run_smoke,
    run_sync,
    start_run,
)

MODEL = "claude-opus-5-5"
HUGE = Usage(0, 200_000, 0, 0)  # 200K output tokens: $4 at full price, more than any cap


def jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """CLI runs poll with --poll-seconds >= 1; never actually sleep in tests."""
    slept: list[float] = []
    monkeypatch.setattr(runner_mod.time, "sleep", slept.append)
    return slept


@pytest.fixture
def env(tmp_path: Path):
    plots = tmp_path / "plots"
    write_plots(plots, [ingest_plot("Q1", 11), ingest_plot("Q2", 12), ingest_plot("Q3", 13)])
    cfg = RunConfig(model=MODEL, out_root=tmp_path / "annotations")
    return plots, cfg


def prepare(plots: Path, cfg: RunConfig, mode: str, budget: float, limit: int | None = None):
    prompt = load_prompt(DEFAULT_PROMPT_VERSION)
    plan = plan_run(load_plots(plots).plots, cfg, prompt, limit=limit)
    return plan, start_run(mode, cfg, prompt, plan, budget_usd=budget)


def cli(args: list[str], api_factory=None) -> tuple[int, str]:
    lines: list[str] = []

    def no_api():
        raise AssertionError("no API client expected")

    code = main(args, api_factory=api_factory or no_api, out=lines.append)
    return code, "\n".join(lines)


def base(cmd: str, plots: Path, cfg: RunConfig, *extra: str) -> list[str]:
    return [cmd, "--model", MODEL, "--plots-dir", str(plots), "--out-dir", str(cfg.out_root),
            *extra]


# --- B1: the worst-case bound ------------------------------------------------------------------


def test_worst_case_request_is_cache_write_plus_input_plus_max_tokens() -> None:
    spec = MODELS[MODEL]
    got = worst_case_request_usd(
        MODEL, static_tokens=8000, variable_tokens=2000, max_tokens=16000, batch=False
    )
    want = (
        math.ceil(8000 * INPUT_ESTIMATE_MARGIN) * spec.input_per_mtok * 1.25
        + math.ceil(2000 * INPUT_ESTIMATE_MARGIN) * spec.input_per_mtok
        + 16000 * spec.output_per_mtok
    ) / 1e6
    assert got == pytest.approx(want)
    batch = worst_case_request_usd(
        MODEL, static_tokens=8000, variable_tokens=2000, max_tokens=16000, batch=True
    )
    assert batch == pytest.approx(want / 2)


def test_worst_case_counts_every_attempt_and_one_title_retries() -> None:
    kw = dict(static_tokens=8000, variable_tokens=(1000, 2000), batch=True, caching=True)
    one = estimate(MODEL, 1, max_attempts=1, **kw)
    three = estimate(MODEL, 1, max_attempts=3, **kw)
    assert three.usd_worst_case == pytest.approx(3 * one.usd_worst_case, rel=1e-3)
    assert three.usd_worst_case > three.usd_high  # the old high end ignored retries for 1 title


def test_pilot_projection_worst_case_is_far_above_the_old_high_end() -> None:
    static = load_prompt(DEFAULT_PROMPT_VERSION).body
    rows = projection_table(math.ceil(len(static) / 3.5), [MODEL])
    pilot = next(r for r in rows if r.titles == 500 and r.caching)
    # QA's numbers: high end ~$39 (under $50), worst case ~$282 (over $50)
    assert pilot.usd_high < 50 < pilot.usd_worst_case
    assert pilot.usd_worst_case > 240


def test_preflight_refuses_a_budget_below_the_worst_case(env) -> None:
    plots, cfg = env
    api = FakeAPI([response()])
    with pytest.raises(SystemExit, match="worst case"):
        cli(base("single", plots, cfg, "--title", "movie:11", "--budget-usd", "0.5"),
            api_factory=lambda: api)
    assert api.created == [] and not cfg.out_root.exists()


def test_approval_note_keys_off_the_worst_case(env, monkeypatch) -> None:
    plots, cfg = env
    monkeypatch.setattr(cli_mod, "APPROVAL_THRESHOLD_USD", 0.5)
    code, text = cli(base("single", plots, cfg, "--title", "movie:11", "--dry-run"))
    assert code == 0 and "worst case over $0" in text and "Hari must approve" in text
    assert "worst case $" in text


def test_sync_run_stops_when_measured_spend_would_exceed_the_budget(env) -> None:
    """QA repro: three invalid responses used to spend $1.02 against a $0.20 budget."""
    plots, cfg = env
    plan, _ = prepare(plots, cfg, "single", budget=100, limit=1)
    worst = request_worst_usd(plan.todo[0], cfg, batch=False)
    plan, run = prepare(plots, cfg, "single", budget=3 * worst + 0.01, limit=1)
    api = FakeAPI([response(invalid_output(), usage=HUGE) for _ in range(3)])
    status = run_sync(api, run, plan.todo, cfg)
    assert len(api.created) == 1  # the 2nd request would have gone over
    assert status.startswith("stopped: budget")
    manifest = run.read_json("manifest.json")
    assert manifest["status"] == status and manifest["unfinished"] == ["movie:11"]
    cost = run.read_json("cost.json")
    assert cost["status"] == status and cost["usd_total"] == pytest.approx(
        price_usage(MODEL, HUGE, batch=False), rel=1e-4
    )


def test_sync_run_stops_before_the_next_title(env) -> None:
    plots, cfg = env
    plan, _ = prepare(plots, cfg, "single", budget=100)
    worst = sum(request_worst_usd(p, cfg, batch=False) for p in plan.todo)
    plan, run = prepare(plots, cfg, "single", budget=worst * 3)
    api = FakeAPI([response(usage=HUGE), response(usage=HUGE), response()])
    status = run_sync(api, run, plan.todo, cfg)
    assert status.startswith("stopped: budget")
    assert len(jsonl(run.file("annotations.jsonl"))) == len(api.created)
    assert run.read_json("manifest.json")["unfinished"] == [
        p.key for p in plan.todo[len(api.created):]
    ]


def test_batch_round_is_not_submitted_over_budget(env) -> None:
    plots, cfg = env
    plan, _ = prepare(plots, cfg, "batch", budget=100)
    worst = sum(request_worst_usd(p, cfg, batch=True) for p in plan.todo)
    plan, run = prepare(plots, cfg, "batch", budget=worst * 3)

    def script(round_no, requests):
        return [BatchResult(cid, "succeeded", response(invalid_output(), usage=HUGE))
                for cid, _ in requests]

    api = FakeAPI(batch_script=script, polls_before_end=0)
    status = run_batch(api, run, plan.todo, cfg, poll_seconds=0, sleep=lambda s: None,
                       log=lambda s: None)
    assert len(api.submitted) == 1 and status.startswith("stopped: budget")
    assert "round 2" in status
    assert run.read_json("cost.json")["requests"] == 3


def test_runner_refuses_a_run_without_a_budget(env) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "single", budget=10, limit=1)
    manifest = run.read_json("manifest.json")
    del manifest["budget_usd"]
    run.write_json("manifest.json", manifest)
    with pytest.raises(ValueError, match="no budget_usd"):
        run_sync(FakeAPI([response()]), run, plan.todo, cfg)


# --- B1.3 / B3: argument validation --------------------------------------------------------


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-1", "abc"])
def test_budget_must_be_finite_and_positive(env, value, capsys) -> None:
    plots, cfg = env
    with pytest.raises(SystemExit) as e:
        cli(base("single", plots, cfg, "--title", "movie:11", "--budget-usd", value))
    assert e.value.code == 2  # argparse error, before anything else runs


@pytest.mark.parametrize(
    "flag, value", [("--max-tokens", "16001"), ("--max-tokens", "0"), ("--max-attempts", "4"),
                    ("--max-attempts", "0")]
)
def test_max_tokens_and_attempts_are_capped(env, flag, value) -> None:
    plots, cfg = env
    with pytest.raises(SystemExit) as e:
        cli(base("single", plots, cfg, "--title", "movie:11", "--dry-run", flag, value))
    assert e.value.code == 2


def test_start_run_rejects_non_finite_budget(env) -> None:
    plots, cfg = env
    for bad in (float("nan"), float("inf"), 0.0):
        with pytest.raises(ValueError):
            prepare(plots, cfg, "single", budget=bad, limit=1)


# --- B2: resume keeps the budget -----------------------------------------------------------


def _resume_setup(plots: Path, cfg: RunConfig):
    """Round 1 all invalid; round 2 is interrupted while polling. The budget covers the
    preflight worst case, but round 2 comes back with huge usage."""
    plan, _ = prepare(plots, cfg, "batch", budget=100)
    worst = sum(request_worst_usd(p, cfg, batch=True) for p in plan.todo)
    plan, run = prepare(plots, cfg, "batch", budget=round(worst * 3 + 0.01, 4))

    def script(round_no, requests):
        usage = HUGE if round_no == 2 else Usage(10, 10, 0, 0)
        return [BatchResult(cid, "succeeded", response(invalid_output(), usage=usage))
                for cid, _ in requests]

    api = FakeAPI(batch_script=script, polls_before_end=1)
    # round 1 collected; round 2 submitted, then the process "dies" while polling
    with pytest.raises(TimeoutError):
        run_batch(api, run, plan.todo, cfg, poll_seconds=0, sleep=lambda s: None,
                  log=lambda s: None, max_polls=1)
    # first call: round 1 polled once then timed out. Poll round 1 to the end and submit 2.
    with pytest.raises(TimeoutError):
        run_batch(api, run, plan.todo, cfg, poll_seconds=0, sleep=lambda s: None,
                  log=lambda s: None, max_polls=1)
    assert len(api.submitted) == 2
    return run, api


def test_resume_without_budget_flag_uses_the_saved_budget_and_checks_it(env) -> None:
    plots, cfg = env
    run, api = _resume_setup(plots, cfg)
    saved = run.read_json("manifest.json")["budget_usd"]
    code, text = cli(
        ["batch", "--resume", run.run_id, "--plots-dir", str(plots), "--out-dir",
         str(cfg.out_root), "--poll-seconds", "1"],
        api_factory=lambda: api,
    )
    assert f"budget ${saved:.4f}" in text
    assert code == cli_mod.EXIT_STOPPED
    assert len(api.submitted) == 2  # round 3 refused: round 2's measured spend is too high
    manifest = run.read_json("manifest.json")
    assert manifest["status"].startswith("stopped: budget") and "round 3" in manifest["status"]
    assert sorted(manifest["unfinished"]) == ["movie:11", "movie:12", "movie:13"]
    assert run.read_json("cost.json")["requests"] == 6


def test_resume_never_raises_the_budget_silently(env) -> None:
    plots, cfg = env
    run, api = _resume_setup(plots, cfg)
    args = ["batch", "--resume", run.run_id, "--plots-dir", str(plots), "--out-dir",
            str(cfg.out_root), "--poll-seconds", "1"]
    with pytest.raises(SystemExit, match="above the run's saved budget"):
        cli([*args, "--budget-usd", "1000"], api_factory=lambda: api)
    code, text = cli([*args, "--budget-usd", "1000", "--allow-budget-increase"],
                     api_factory=lambda: api)
    assert "WARNING: raising this run's budget" in text
    manifest = run.read_json("manifest.json")
    assert manifest["budget_usd"] == 1000 and manifest["budget_history"][0]["to"] == 1000


def test_resume_with_a_lower_budget_uses_it(env) -> None:
    plots, cfg = env
    run, api = _resume_setup(plots, cfg)
    cli(["batch", "--resume", run.run_id, "--plots-dir", str(plots), "--out-dir",
         str(cfg.out_root), "--poll-seconds", "1", "--budget-usd", "0.01"],
        api_factory=lambda: api)
    assert run.read_json("manifest.json")["budget_usd"] == 0.01


def test_resume_of_an_old_run_without_saved_budget_needs_the_flag(env) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "batch", budget=10)
    manifest = run.read_json("manifest.json")
    del manifest["budget_usd"]
    run.write_json("manifest.json", manifest)
    with pytest.raises(SystemExit, match="no saved budget"):
        cli(["batch", "--resume", run.run_id, "--plots-dir", str(plots), "--out-dir",
             str(cfg.out_root)], api_factory=FakeAPI)


# --- minor 2: a round collected twice doesn't duplicate records ------------------------------


def test_recollecting_an_interrupted_round_writes_nothing_twice(env) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "batch", budget=100)
    api = FakeAPI(
        batch_script=lambda n, reqs: [BatchResult(c, "succeeded", response()) for c, _ in reqs],
        polls_before_end=0,
    )
    run_batch(api, run, plan.todo, cfg, poll_seconds=0, sleep=lambda s: None, log=lambda s: None)
    # simulate a crash after collecting but before the manifest said so
    manifest = run.read_json("manifest.json")
    manifest["rounds"][0]["collected"] = False
    manifest["status"] = "round 1 submitted"
    run.write_json("manifest.json", manifest)
    run_batch(api, RunDir(cfg.out_root, run.run_id), plan.todo, cfg, poll_seconds=0,
              sleep=lambda s: None, log=lambda s: None)
    assert len(jsonl(run.file("annotations.jsonl"))) == 3
    assert len(jsonl(run.file("attempts.jsonl"))) == 3
    assert len(api.submitted) == 1


# --- M5: API errors ------------------------------------------------------------------------


def test_transient_error_is_logged_reserved_and_retried(env) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "single", budget=100, limit=1)
    worst = request_worst_usd(plan.todo[0], cfg, batch=False)
    api = FakeAPI([TransientAPIError("timeout: x", possibly_billed=True),
                   TransientAPIError("HTTP 429: slow down", possibly_billed=False),
                   response()])
    sleeps: list[float] = []
    status = run_sync(api, run, plan.todo, cfg, sleep=sleeps.append)
    assert status == "complete" and len(api.created) == 3 and len(sleeps) == 2
    attempts = jsonl(run.file("attempts.jsonl"))
    assert attempts[0]["possibly_billed"] is True
    assert attempts[0]["reserved_usd"] == pytest.approx(worst, rel=1e-4)
    assert "reserved_usd" not in attempts[1]
    cost = run.read_json("cost.json")
    assert cost["usd_reserved_possibly_billed"] == pytest.approx(worst, rel=1e-3)
    assert cost["usd_total_with_reserve"] > cost["usd_total"]


def test_reserved_spend_counts_against_the_budget(env) -> None:
    plots, cfg = env
    plan, _ = prepare(plots, cfg, "single", budget=100, limit=1)
    worst = request_worst_usd(plan.todo[0], cfg, batch=False)
    plan, run = prepare(plots, cfg, "single", budget=worst * 1.5, limit=1)
    api = FakeAPI([TransientAPIError("timeout", possibly_billed=True), response()])
    status = run_sync(api, run, plan.todo, cfg, sleep=lambda s: None)
    assert len(api.created) == 1 and status.startswith("stopped: budget")


def test_fatal_error_stops_cleanly(env) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "single", budget=100)
    api = FakeAPI([response(), FatalAPIError("HTTP 401: invalid x-api-key")])
    status = run_sync(api, run, plan.todo, cfg)
    assert status.startswith("stopped: fatal API error: HTTP 401")
    manifest = run.read_json("manifest.json")
    assert manifest["unfinished"] == ["movie:12", "movie:13"]
    assert run.read_json("cost.json")["records_stored"] == 1


def test_consecutive_transient_errors_stop_the_run(env) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "single", budget=100)
    api = FakeAPI([TransientAPIError("HTTP 529: overloaded", possibly_billed=True)] * 5)
    status = run_sync(api, run, plan.todo, cfg, sleep=lambda s: None)
    assert status.startswith("stopped: 5 consecutive transient API errors")
    assert len(api.created) == 5
    assert jsonl(run.file("failures.jsonl"))[0]["title_key"] == "movie:11"  # 3 attempts used


def test_smoke_fatal_error_stops_cleanly(env) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "smoke", budget=100, limit=1)
    report = run_smoke(FakeAPI([FatalAPIError("HTTP 403: no")]), run, plan.todo[0], cfg)
    assert report["status"].startswith("stopped: fatal")
    assert run.read_json("cost.json")["requests"] == 0


def test_batch_submission_failure_stops_and_resume_refuses(env) -> None:
    plots, cfg = env
    plan, run = prepare(plots, cfg, "batch", budget=100)
    api = FakeAPI(batch_script=FatalAPIError("batch submission failed: timeout"))
    status = run_batch(api, run, plan.todo, cfg, poll_seconds=0, log=lambda s: None)
    assert "check the Anthropic Console" in status
    assert run.read_json("cost.json")["status"] == status
    with pytest.raises(RuntimeError, match="paid for twice"):
        run_batch(api, run, plan.todo, cfg, poll_seconds=0, log=lambda s: None)


def _status_error(code: int):
    anthropic = pytest.importorskip("anthropic")
    import httpx2

    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx2.Response(code, request=req, json={"error": {"message": "m"}})
    cls = anthropic.RateLimitError if code == 429 else anthropic.APIStatusError
    return cls("m", response=resp, body=None)


def test_sdk_errors_are_classified() -> None:
    anthropic = pytest.importorskip("anthropic")
    import httpx2

    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    t = classify_sdk_error(anthropic.APITimeoutError(request=req))
    assert isinstance(t, TransientAPIError) and t.possibly_billed
    c = classify_sdk_error(anthropic.APIConnectionError(request=req))
    assert isinstance(c, TransientAPIError) and c.possibly_billed
    r = classify_sdk_error(_status_error(429))
    assert isinstance(r, TransientAPIError) and not r.possibly_billed
    s = classify_sdk_error(_status_error(529))
    assert isinstance(s, TransientAPIError) and s.possibly_billed
    assert isinstance(classify_sdk_error(_status_error(401)), FatalAPIError)
    assert isinstance(classify_sdk_error(_status_error(400)), RequestRejectedError)


class RecordingSdk:
    """A fake anthropic client: records with_options and raises a scripted error."""

    def __init__(self, error=None):
        self.options: list[dict] = []
        self.error = error
        self.messages = NS(create=self._create, batches=NS(create=self._create))

    def with_options(self, **kw):
        self.options.append(kw)
        return self

    def _create(self, **kw):
        raise self.error


def test_billable_calls_run_without_sdk_retries() -> None:
    pytest.importorskip("anthropic")
    sdk = RecordingSdk(_status_error(529))
    api = SdkAnnotationAPI(sdk)
    with pytest.raises(TransientAPIError):
        api.create({"model": MODEL})
    with pytest.raises(FatalAPIError, match="batch submission failed"):
        api.submit_batch([("a", {"model": MODEL})])
    assert sdk.options == [{"max_retries": 0}, {"max_retries": 0}]


# --- minor 1: count_tokens only after the guard ----------------------------------------------


def test_count_tokens_is_not_called_before_the_budget_guard(env) -> None:
    plots, cfg = env
    api = FakeAPI([response()])
    with pytest.raises(SystemExit, match="exceeds"):
        cli(base("single", plots, cfg, "--title", "movie:11", "--budget-usd", "0.01",
                 "--count-tokens"), api_factory=lambda: api)
    assert api.counted == []


# --- M2: --titles-file -----------------------------------------------------------------------


def test_titles_file_shapes(tmp_path: Path) -> None:
    pilot = tmp_path / "pilot_effective.jsonl"
    pilot.write_text('{"qid": "Q2", "bucket": "x"}\n{"qid": "Q1"}\n{"qid": "Q2"}\n')
    assert read_titles_file(pilot) == ["Q2", "Q1"]
    plain = tmp_path / "list.txt"
    plain.write_text("# gold\nmovie:13\n\nQ1\n")
    assert read_titles_file(plain) == ["movie:13", "Q1"]
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing\n")
    with pytest.raises(InputFormatError):
        read_titles_file(empty)


def test_select_plots_by_qid_or_key(env) -> None:
    plots, _ = env
    loaded = load_plots(plots).plots
    chosen, missing = select_plots(loaded, ["movie:13", "Q1", "Q99"])
    assert [p.key for p in chosen] == ["movie:13", "movie:11"] and missing == ["Q99"]


def test_live_batch_needs_a_titles_file(env) -> None:
    plots, cfg = env
    with pytest.raises(SystemExit, match="--titles-file"):
        cli(base("batch", plots, cfg, "--limit", "2", "--budget-usd", "100"),
            api_factory=FakeAPI)


def test_batch_scoped_by_titles_file(env, tmp_path) -> None:
    plots, cfg = env
    sel = tmp_path / "gold_selection.jsonl"
    sel.write_text('{"qid": "Q3"}\n{"qid": "Q404"}\n')
    api = FakeAPI(
        batch_script=lambda n, reqs: [BatchResult(c, "succeeded", response()) for c, _ in reqs],
        polls_before_end=0,
    )
    code, text = cli(base("batch", plots, cfg, "--titles-file", str(sel), "--budget-usd", "100",
                          "--poll-seconds", "1"), api_factory=lambda: api)
    assert code == 0, text
    assert [cid for cid, _ in api.submitted[0]] == ["movie-13-r1"]
    assert "Q404: listed in --titles-file but no usable plot input" in text


# --- minor 7: Phase 1 model lock -------------------------------------------------------------


def test_non_phase1_model_needs_the_override(env) -> None:
    plots, cfg = env
    args = ["single", "--title", "movie:11", "--model", "claude-sonnet-5-5", "--dry-run",
            "--plots-dir", str(plots)]
    with pytest.raises(SystemExit, match="Phase 1"):
        cli(args)
    code, text = cli([*args, "--allow-non-phase1-model"])
    assert code == 0 and "WARNING" in text


# --- nits ------------------------------------------------------------------------------------


def test_dry_run_dump_defaults_inside_the_data_dir(env, tmp_path) -> None:
    plots, _ = env
    out_dir = tmp_path / "data" / "annotations"
    code, text = cli(["batch", "--limit", "2", "--model", MODEL, "--dry-run", "--plots-dir",
                      str(plots), "--out-dir", str(out_dir), "--dump-requests"])
    assert code == 0 and "no run files written" in text and "nothing written" not in text
    dumps = list((tmp_path / "data" / "dry_runs").glob("batch_requests_*.jsonl"))
    assert len(dumps) == 1 and len(jsonl(dumps[0])) == 2
    assert not out_dir.exists()


def test_request_token_split_matches_worst_case_inputs(env) -> None:
    plots, cfg = env
    plan, _ = prepare(plots, cfg, "single", budget=10, limit=1)
    static, variable = request_token_split(plan.todo[0].params)
    assert request_worst_usd(plan.todo[0], cfg, batch=False) == pytest.approx(
        worst_case_request_usd(MODEL, static_tokens=static, variable_tokens=variable,
                               max_tokens=cfg.max_tokens, batch=False)
    )


def test_redirects_to_other_hosts_are_refused() -> None:
    from laminary_pipeline.ingest.http import HostNotAllowed, _AllowlistRedirectHandler

    handler = _AllowlistRedirectHandler()
    req = urllib.request.Request("https://en.wikipedia.org/w/api.php")
    with pytest.raises(HostNotAllowed):
        handler.redirect_request(req, None, 302, "Found", {}, "https://api.themoviedb.org/3/x")
    ok = handler.redirect_request(req, None, 302, "Found", {}, "https://en.wikipedia.org/wiki/X")
    assert ok.full_url == "https://en.wikipedia.org/wiki/X"


def test_conftest_blocks_network_and_key() -> None:
    import os
    import socket

    assert "ANTHROPIC_API_KEY" not in os.environ
    with pytest.raises(RuntimeError, match="network"):
        socket.create_connection(("api.anthropic.com", 443))
    with pytest.raises(RuntimeError, match="network"):
        socket.socket().connect(("127.0.0.1", 9))
    anthropic = pytest.importorskip("anthropic")
    with pytest.raises(RuntimeError, match="anthropic.Anthropic"):
        anthropic.Anthropic(api_key="x")
