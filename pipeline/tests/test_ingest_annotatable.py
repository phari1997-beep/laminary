"""Annotatable, not just passing (QA, 2026-10-02).

29 of 498 pilot titles were backfilled reserves whose plot files came from fetcher 1.0.0 or
1.3.0: they pass the 150-word rule, but every current prompt refuses them (older than 1.4.0),
and nothing re-fetched a passing file. A pilot slot, backfill, the effective pilot, the report
and the gold sheet now count only annotatable files (the gate and the request build pass under
the default prompt), and any pre-1.4.0 file is fetched again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from annotate_support import ingest_plot

from laminary_pipeline.annotate.ready import not_annotatable_reason
from laminary_pipeline.ingest.paths import DataPaths, write_json_atomic
from laminary_pipeline.ingest.plots import (
    effective_pilot,
    format_report,
    needs_fetch,
    not_annotatable,
    plots_report,
    run_plots,
)


def ok_film(qid: str, version: str = "1.4.0") -> dict[str, Any]:
    return {**ingest_plot(qid, tmdb_id=int(qid[1:])), "fetcher_version": version,
            "word_count": 200, "section": {"heading": "plot"},
            "permalink": f"https://en.wikipedia.org/w/index.php?title={qid}&oldid=1"}


def cands(rows: list[tuple[str, str, int]]) -> list[dict[str, Any]]:
    return [{"qid": q, "title": f"Film {q}", "year": 2019, "media_type": "movie",
             "region": "english", "language": "english", "decade": "2010s",
             "bucket": "film:english", "role": role, "bucket_rank": rank}
            for q, role, rank in rows]


class Fetcher:
    def __init__(self) -> None:
        self.fetched: list[str] = []

    def fetch(self, cand: dict[str, Any]) -> dict[str, Any]:
        self.fetched.append(cand["qid"])
        return ok_film(cand["qid"])


def test_a_passing_pre_1_4_0_file_is_not_annotatable() -> None:
    assert not_annotatable_reason(ok_film("Q11")) is None
    for version in ("1.0.0", "1.3.0", None):
        rec = ok_film("Q11", version or "1.4.0")
        if version is None:
            del rec["fetcher_version"]
        reason = not_annotatable_reason(rec)
        assert reason is not None and "needs 1.4.0" in reason
    assert not_annotatable_reason({"status": "skipped", "skip_reason": "too_short"}) == (
        "skipped: too_short")
    no_tmdb = ok_film("Q11")
    no_tmdb["candidate"]["tmdb_id"] = None
    assert "tmdb_id" in (not_annotatable_reason(no_tmdb) or "")


def test_pre_1_4_0_files_are_fetched_again_passing_or_not(tmp_path: Path) -> None:
    paths = DataPaths.resolve(str(tmp_path))
    for version in ("1.0.0", "1.3.0"):
        write_json_atomic(paths.plot_file("Q11"), ok_film("Q11", version))
        assert needs_fetch(paths, "Q11", False), version
    write_json_atomic(paths.plot_file("Q11"), ok_film("Q11", "1.4.0"))
    assert not needs_fetch(paths, "Q11", False)


def test_effective_pilot_backfill_and_report_count_annotatable_titles(tmp_path: Path) -> None:
    """Pilot: Q11 annotatable, Q12 passing but from fetcher 1.3.0. The reserve Q21 is from
    1.0.0 (passing). A plain run re-fetches Q12; backfill re-fetches Q21 too and never counts
    an old file as filling a slot."""
    paths = DataPaths.resolve(str(tmp_path))
    rows = cands([("Q11", "pilot", 1), ("Q12", "pilot", 2), ("Q21", "reserve", 1),
                  ("Q22", "reserve", 2)])
    write_json_atomic(paths.plot_file("Q11"), ok_film("Q11"))
    write_json_atomic(paths.plot_file("Q12"), ok_film("Q12", "1.3.0"))
    write_json_atomic(paths.plot_file("Q21"), ok_film("Q21", "1.0.0"))
    assert [r["qid"] for r in effective_pilot(paths, rows)] == ["Q11"]
    report = plots_report(paths, rows)
    assert report["overall"]["ok"] == 3  # the 150-word rule alone
    assert report["annotatable"]["ok"] == 1
    assert report["annotatable"]["not_annotatable_titles"] == ["Q12", "Q21"]
    assert report["annotatable"]["not_annotatable_reasons"] == {
        "plot file from before fetcher 1.4.0": 2}
    assert report["effective_pilot"]["by_bucket"]["film:english"] == "1/2"
    assert "2 pass the 150-word rule but can't be sent" in format_report(report)
    assert "needs 1.4.0" in (not_annotatable(paths, "Q12") or "")

    # a plain run re-fetches the pilot's old file; Q12 then fills its slot
    fetcher = Fetcher()
    run_plots(paths, rows, fetcher, log=lambda m: None)  # type: ignore[arg-type]
    assert fetcher.fetched == ["Q12"]
    assert [r["qid"] for r in effective_pilot(paths, rows)] == ["Q11", "Q12"]

    # backfill: Q12 is old again and fails; the old reserve Q21 does not count as filling it
    write_json_atomic(paths.plot_file("Q12"), {"qid": "Q12", "status": "skipped",
                                               "skip_reason": "too_short",
                                               "fetcher_version": "1.5.6",
                                               "candidate": {"media_type": "movie"}})
    fetcher = Fetcher()
    run_plots(paths, rows, fetcher, backfill=True, log=lambda m: None)  # type: ignore[arg-type]
    assert fetcher.fetched == ["Q21"]  # re-fetched, now annotatable, fills the slot
    assert [r["qid"] for r in effective_pilot(paths, rows)] == ["Q11", "Q21"]


def test_reasons_are_grouped_without_the_title_key() -> None:
    from laminary_pipeline.ingest.plots import _reason_group

    assert _reason_group("movie:12: plot file from fetcher '1.3.0', annotate-1.2.0 needs "
                         "1.4.0 or later: re-run ingest plots --refresh") == (
        "plot file from before fetcher 1.4.0")
    assert _reason_group("tv_series:9: no integer tmdb_id (LLM records require one)") == (
        "no integer tmdb_id (LLM records require one)")
    assert _reason_group("Q5: season numbers [1, 1] are not in increasing order") == (
        "season numbers [1, 1] are not in increasing order")


def test_held_priority_series_log_names_the_not_annotatable_reason(tmp_path: Path) -> None:
    from laminary_pipeline.ingest.wikipedia import FETCHER_VERSION

    paths = DataPaths.resolve(str(tmp_path))
    rows = [{**c, "title": "Seinfeld"} for c in cands([("Q23733", "pilot", 1)])]
    rec = ok_film("Q23733", FETCHER_VERSION)
    rec["candidate"]["tmdb_id"] = None
    write_json_atomic(paths.plot_file("Q23733"), rec)
    logs: list[str] = []
    run_plots(paths, rows, Fetcher(), backfill=True, log=logs.append)  # type: ignore[arg-type]
    warning = [m for m in logs if m.startswith("WARNING for Hari: priority series Q23733")]
    assert warning and "is not annotatable (no integer tmdb_id" in warning[0]
    assert "None:" not in warning[0]
