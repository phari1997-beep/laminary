"""End-to-end CLI runs against the recorded fixtures: candidates -> plots -> report.

Also: dry runs make no network calls and write nothing, runs are resumable, TMDB is never
contacted, and the data directory is gitignored.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from laminary_pipeline.ingest.__main__ import main
from laminary_pipeline.ingest.paths import DEFAULT_DATA_DIR, PIPELINE_DIR, read_jsonl
from wikimedia_fake import FakeWikimedia

NOW = "2026-09-30T12:00:00Z"
REPO_ROOT = PIPELINE_DIR.parent


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt and braces: any real HTTP or socket connection fails the test."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"real network access attempted: {args[:1]}")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    (d / "config").mkdir(parents=True)
    (d / "config" / "gold_seed_titles.csv").write_text(
        "title,year,media_type,region,guessed_plot,guessed_arc,note\n"
        "Lantern Keeper,1994,movie,english,rebirth,man_in_a_hole,\n",
        encoding="utf-8",
    )
    return d


def run(data_dir: Path, *argv: str, fake: FakeWikimedia | None = None) -> tuple[int, str]:
    out: list[str] = []
    code = main(["--data-dir", str(data_dir), *argv], transport=fake, clock=lambda: NOW,
                sleep=lambda s: None, log=out.append)
    return code, "\n".join(out)


def test_end_to_end_candidates_plots_report(data_dir: Path) -> None:
    fake = FakeWikimedia()
    code, out = run(data_dir, "candidates", fake=fake)
    assert code == 0, out
    rows = list(read_jsonl(data_dir / "pilot_candidates.jsonl"))
    pilot = {r["qid"]: r for r in rows if r["role"] == "pilot"}
    assert set(pilot) == {"Q9000001", "Q9000002", "Q9000003", "Q9000004"}  # documentary excluded
    assert pilot["Q9000001"]["gold_seed"] and pilot["Q9000001"]["tmdb_id"] == 90001
    assert pilot["Q9000003"]["series_status"] == "ended"
    summary = json.loads((data_dir / "reports" / "candidates_summary.json").read_text())
    assert summary["excluded"] == {"genre:documentary": 1} and summary["seeds_missing"] == []

    code, out = run(data_dir, "plots", fake=fake)
    assert code == 0, out
    status = {q: json.loads((data_dir / "plots" / f"{q}.json").read_text()) for q in pilot}
    assert status["Q9000001"]["status"] == "ok" and status["Q9000003"]["status"] == "ok"
    assert status["Q9000002"]["skip_reason"] == "too_short"
    assert status["Q9000004"]["skip_reason"] == "qid_mismatch"
    report = json.loads((data_dir / "reports" / "plots_summary.json").read_text())
    assert report["overall"] == {"ok": 2, "total": 4, "pass_rate": 0.5}
    assert report["by_media_type"]["movie"]["ok"] == 1
    assert report["by_media_type"]["tv_series"] == {"ok": 1, "total": 2, "pass_rate": 0.5}
    assert report["by_language"]["tamil"] == {"ok": 0, "total": 1, "pass_rate": 0.0}
    assert report["by_decade"]["1990s"]["ok"] == 1
    assert report["skip_reasons"] == {"qid_mismatch": 1, "too_short": 1}
    assert "pass the 150-word rule" in out and "By language" in out

    # No TMDB host (or anything but the three Wikimedia hosts) was contacted.
    assert fake.hosts <= {"en.wikipedia.org", "query.wikidata.org", "www.wikidata.org"}
    assert not any("themoviedb" in r.url or "tmdb" in r.host for r in fake.requests)


def test_plots_are_resumable_and_retry_fetch_errors(data_dir: Path) -> None:
    fake = FakeWikimedia()
    run(data_dir, "candidates", fake=fake)
    run(data_dir, "plots", fake=fake)
    again = FakeWikimedia()
    code, out = run(data_dir, "plots", fake=again)
    assert code == 0 and again.requests == []  # nothing re-fetched
    # A transient failure is retried on the next run.
    path = data_dir / "plots" / "Q9000002.json"
    path.write_text(json.dumps({"qid": "Q9000002", "status": "skipped",
                                "skip_reason": "fetch_error"}))
    code, out = run(data_dir, "plots", fake=FakeWikimedia())
    assert "Q9000002 'Nizhal Veedu': too_short" in out  # re-fetched (served from the cache)
    assert json.loads(path.read_text())["skip_reason"] == "too_short"


def test_plots_limit_and_qid(data_dir: Path) -> None:
    fake = FakeWikimedia()
    run(data_dir, "candidates", fake=fake)
    run(data_dir, "plots", "--limit", "1", fake=fake)
    assert len(list((data_dir / "plots").glob("Q*.json"))) == 1
    run(data_dir, "plots", "--qid", "Q9000003", fake=fake)
    assert (data_dir / "plots" / "Q9000003.json").exists()
    assert len(list((data_dir / "plots").glob("Q*.json"))) == 2


def test_backfill_uses_reserves(data_dir: Path) -> None:
    fake = FakeWikimedia()
    run(data_dir, "candidates", fake=fake)
    path = data_dir / "pilot_candidates.jsonl"
    rows = list(read_jsonl(path))
    # Make the too-short Tamil title the pilot and the Lantern Keeper a Tamil reserve.
    for r in rows:
        if r["qid"] == "Q9000001":
            r.update(bucket="film:tamil", role="reserve", bucket_rank=1)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    code, out = run(data_dir, "plots", "--backfill", fake=fake)
    assert code == 0
    assert "[backfill film:tamil] Q9000001" in out
    report = json.loads((data_dir / "reports" / "plots_summary.json").read_text())
    assert report["effective_pilot"]["by_bucket"]["film:tamil"] == "1/1"


def test_dry_runs_are_offline_and_write_nothing(data_dir: Path) -> None:
    fake = FakeWikimedia()
    code, out = run(data_dir, "candidates", "--dry-run", fake=fake)
    assert code == 0 and fake.requests == []
    assert "not cached yet" in out
    assert not (data_dir / "pilot_candidates.jsonl").exists()

    code, out = run(data_dir, "plots", "--dry-run", fake=fake)
    assert code == 0 and "run the 'candidates' command first" in out

    run(data_dir, "candidates", fake=FakeWikimedia())
    before = (data_dir / "pilot_candidates.jsonl").read_text()
    dry = FakeWikimedia()
    code, out = run(data_dir, "candidates", "--dry-run", "--limit", "2", fake=dry)
    assert code == 0 and dry.requests == [] and '"pilot": 2' in out
    assert (data_dir / "pilot_candidates.jsonl").read_text() == before

    code, out = run(data_dir, "plots", "--dry-run", fake=dry)
    assert code == 0 and dry.requests == [] and "would fetch 4" in out
    assert not (data_dir / "plots").exists()


def test_offline_flag_serves_from_cache_only(data_dir: Path) -> None:
    run(data_dir, "candidates", fake=FakeWikimedia())
    offline = FakeWikimedia()
    code, _ = run(data_dir, "--offline", "candidates", fake=offline)
    assert code == 0 and offline.requests == []


def test_ingest_and_gold_code_never_mention_tmdb_hosts() -> None:
    pkg = PIPELINE_DIR / "laminary_pipeline"
    for sub in ("ingest", "gold"):
        for path in (pkg / sub).rglob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            assert "themoviedb" not in text and "api.tmdb" not in text, path


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_data_dir_is_gitignored_except_config() -> None:
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    rel = DEFAULT_DATA_DIR.relative_to(REPO_ROOT)

    def ignored(path: str) -> bool:
        res = subprocess.run(["git", "-C", str(REPO_ROOT), "check-ignore", "-q", "--no-index",
                              str(rel / path)], capture_output=True)
        return res.returncode == 0

    for p in ("pilot_candidates.jsonl", "plots/Q1.json", "cache/http/ab/x.json",
              "reports/plots_summary.json", "gold/gold_labels.jsonl",
              "gold/gold_labels_template.csv", "anything_else.csv"):
        assert ignored(p), p
    for p in ("README.md", "config/gold_seed_titles.csv", "config/similarity_pairs.csv"):
        assert not ignored(p), p
