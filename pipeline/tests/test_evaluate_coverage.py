"""QA M4: source-mismatched titles are excluded from the exit metric and reported separately,
and the metric only passes with enough of the gold set scored (proposed thresholds)."""

from __future__ import annotations

from annotate_support import STORY, gold_from_example
from test_evaluate import model_record

from laminary_pipeline.evaluate.report import (
    EXIT_TARGET,
    MIN_SCORED_FRACTION,
    MIN_SCORED_TITLES,
    evaluate,
    render_markdown,
)

OTHER_TEXT = STORY + " A different revision added this sentence."


def gold_set(n: int, *, wrong: int = 0, mismatched: int = 0):
    """n matrix titles; the first ``mismatched`` gold labels were made from other text, the
    next ``wrong`` model records disagree with gold."""
    gold, model = [], []
    for i in range(n):
        tmdb = 1000 + i
        text = OTHER_TEXT if i < mismatched else STORY
        primary = "quest" if mismatched <= i < mismatched + wrong else None
        gold.append(gold_from_example("the_matrix", tmdb, text=text, primary=primary))
        model.append(model_record(tmdb, "the_matrix"))
    return gold, model


def test_thresholds_are_the_proposed_values() -> None:
    assert (MIN_SCORED_TITLES, MIN_SCORED_FRACTION, EXIT_TARGET) == (80, 0.90, 0.80)


def test_small_sample_cannot_pass() -> None:
    """QA: 4/5 used to pass."""
    gold, model = gold_set(5, wrong=1)
    h = evaluate(gold, model)["headline"]
    assert (h["correct"], h["total"]) == (4, 5) and h["meets_target"] is True
    assert h["coverage"]["passes"] is False and h["passes"] is False


def test_full_coverage_passes() -> None:
    gold, model = gold_set(80, wrong=16)
    h = evaluate(gold, model)["headline"]
    assert h["accuracy"] == 0.8 and h["coverage"]["fraction"] == 1.0 and h["passes"] is True


def test_mismatched_titles_leave_the_denominator_and_are_listed() -> None:
    gold, model = gold_set(85, mismatched=5)
    report = evaluate(gold, model)
    h = report["headline"]
    assert h["total"] == 80 and h["correct"] == 80
    assert h["excluded_source_mismatch"] == [f"movie:{1000 + i}" for i in range(5)]
    assert report["counts"]["source_hash_mismatches"] == h["excluded_source_mismatch"]
    assert h["coverage"]["gold_annotated"] == 85
    assert h["passes"] is True  # 80 scored, 94% of the gold set
    assert all(r["title_key"] not in h["excluded_source_mismatch"] for r in report["per_title"])
    md = render_markdown(report)
    assert "excluded from every score" in md and "Coverage: 80 of 85" in md


def test_too_many_mismatches_fail_coverage() -> None:
    gold, model = gold_set(100, mismatched=15)
    h = evaluate(gold, model)["headline"]
    assert h["accuracy"] == 1.0 and h["coverage"]["fraction"] == 0.85
    assert h["passes"] is False
