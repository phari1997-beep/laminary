"""Phase 1 exit gate (DECISIONS 2026-09-30): A (overall), B (shown labels) and coverage, plus
double labeling. Source-mismatched titles are excluded and reported separately (QA M4)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from annotate_support import STORY, gold_from_example
from test_evaluate import model_record

from laminary_pipeline.annotation import DISPLAY_CONFIDENCE_THRESHOLD
from laminary_pipeline.evaluate.report import (
    HUMAN_MARGIN,
    MIN_DOUBLE_LABELED,
    MIN_SCORED_FRACTION,
    MIN_SCORED_TITLES,
    MIN_SHOWN_TITLES,
    OVERALL_TARGET,
    SHOWN_TARGET,
    EvaluationInputError,
    evaluate,
    index_gold,
    render_markdown,
)

OTHER_TEXT = STORY + " A different revision added this sentence."
SHOWN = 0.97  # at or above the display threshold
HIDDEN = 0.80  # below it


def gold_set(
    n: int,
    *,
    wrong: int = 0,
    mismatched: int = 0,
    conf: float = SHOWN,
    wrong_conf: float | None = None,
) -> tuple[list[dict], list[dict]]:
    """n matrix titles; the first ``mismatched`` gold labels were made from other text, the
    next ``wrong`` model records disagree with gold. Model primary confidence is ``conf``
    (``wrong_conf`` for the wrong ones, if given)."""
    gold, model = [], []
    for i in range(n):
        tmdb = 1000 + i
        text = OTHER_TEXT if i < mismatched else STORY
        is_wrong = mismatched <= i < mismatched + wrong
        primary = "the_quest" if is_wrong else None
        gold.append(gold_from_example("the_matrix", tmdb, text=text, primary=primary))
        rec = model_record(tmdb, "the_matrix")
        c = wrong_conf if is_wrong and wrong_conf is not None else conf
        rec["layers"]["archetypal_plot"]["primary"]["confidence"] = c
        model.append(rec)
    return gold, model


def second_label(
    rec: dict[str, Any], *, labeler: str = "L02", primary: str | None = None,
    abstain: bool = False,
) -> dict[str, Any]:
    out = copy.deepcopy(rec)
    out["provenance"]["annotator"]["labeler_id"] = labeler
    if abstain:
        out.update(outcome="abstained", abstain_reason="summary_too_thin")
        out.pop("layers", None)
        out.pop("beat_tags", None)
    elif primary:
        out["layers"]["archetypal_plot"]["primary"]["label"] = primary
    return out


def with_double_labels(gold: list[dict], n: int, disagree: int) -> list[dict]:
    """Second-labeler records for the first ``n`` gold titles; the first ``disagree`` differ."""
    seconds = [
        second_label(g, primary="mystery" if i < disagree else None)
        for i, g in enumerate(gold[:n])
    ]
    return gold + seconds


def test_gate_constants() -> None:
    assert (OVERALL_TARGET, HUMAN_MARGIN, MIN_DOUBLE_LABELED) == ("0.85", "0.05", 20)
    assert (SHOWN_TARGET, MIN_SHOWN_TITLES) == ("0.95", 30)
    assert (MIN_SCORED_TITLES, MIN_SCORED_FRACTION) == (80, "0.95")  # DECISIONS 2026-10-02
    assert DISPLAY_CONFIDENCE_THRESHOLD == 0.95


# --- A: overall ------------------------------------------------------------------------------


def test_a_passes_via_target_exactly_at_85_percent() -> None:
    gold, model = gold_set(100, wrong=15, wrong_conf=HIDDEN)
    h = evaluate(gold, model)["headline"]
    a = h["overall"]
    assert (a["correct"], a["total"]) == (85, 100)
    assert a["meets_target"] is True and a["passes"] is True  # exact, no float slop
    assert a["human_n"] == 0 and a["human_bar"] is None and a["meets_human_bar"] is False
    assert h["shown_labels"]["status"] == "pass"  # the 15 misses were all hidden
    assert h["coverage"]["passes"] is True and h["passes"] is True


def test_a_fails_below_target_without_double_labels() -> None:
    gold, model = gold_set(100, wrong=16, wrong_conf=HIDDEN)
    h = evaluate(gold, model)["headline"]
    assert h["overall"]["passes"] is False and h["passes"] is False
    assert h["shown_labels"]["passes"] is True and h["coverage"]["passes"] is True


def test_a_passes_via_human_agreement() -> None:
    """80% model accuracy; humans agree on 17/20 = 85%, so the bar is 80%."""
    gold, model = gold_set(100, wrong=20, wrong_conf=HIDDEN)
    report = evaluate(with_double_labels(gold, MIN_DOUBLE_LABELED, disagree=3), model)
    a = report["headline"]["overall"]
    assert a["accuracy"] == 0.8 and a["meets_target"] is False
    assert (a["human_n"], a["human_agreement"]) == (20, 0.85)
    assert a["human_bar"] == pytest.approx(0.80)
    assert a["meets_human_bar"] is True and a["passes"] is True
    assert report["headline"]["passes"] is True
    md = render_markdown(report)
    assert "Human-human agreement 85.0% on 20 double-labeled titles; bar 80.0%: met" in md


def test_a_human_bar_needs_twenty_double_labeled_titles() -> None:
    gold, model = gold_set(100, wrong=20, wrong_conf=HIDDEN)
    a = evaluate(with_double_labels(gold, 19, disagree=3), model)["headline"]["overall"]
    assert a["human_n"] == 19 and a["human_agreement"] is not None
    assert a["human_bar"] is None and a["passes"] is False


def test_a_fails_when_below_human_bar() -> None:
    """Humans agree on 19/20 = 95%, bar 90%; the model's 80% misses both routes."""
    gold, model = gold_set(100, wrong=20, wrong_conf=HIDDEN)
    a = evaluate(with_double_labels(gold, 20, disagree=1), model)["headline"]["overall"]
    assert a["human_bar"] == pytest.approx(0.90)
    assert a["meets_target"] is False and a["meets_human_bar"] is False and a["passes"] is False


# --- B: shown labels -------------------------------------------------------------------------


def test_b_passes() -> None:
    """30 shown labels, 29 right (96.7%); the 70 others are hidden."""
    gold, model = gold_set(100, wrong=1, conf=HIDDEN, wrong_conf=SHOWN)
    for rec in model[1:30]:
        rec["layers"]["archetypal_plot"]["primary"]["confidence"] = SHOWN
    b = evaluate(gold, model)["headline"]["shown_labels"]
    assert (b["correct"], b["total"], b["status"], b["passes"]) == (29, 30, "pass", True)


def test_b_fails() -> None:
    gold, model = gold_set(100, wrong=2, conf=HIDDEN, wrong_conf=SHOWN)
    for rec in model[2:30]:
        rec["layers"]["archetypal_plot"]["primary"]["confidence"] = SHOWN
    h = evaluate(gold, model)["headline"]
    b = h["shown_labels"]
    assert (b["correct"], b["total"], b["status"], b["passes"]) == (28, 30, "fail", False)
    assert h["overall"]["passes"] is True and h["passes"] is False


def test_b_insufficient_data_does_not_pass() -> None:
    """29 shown labels, all right: still not enough to pass."""
    gold, model = gold_set(100, conf=HIDDEN)
    for rec in model[:29]:
        rec["layers"]["archetypal_plot"]["primary"]["confidence"] = SHOWN
    report = evaluate(gold, model)
    h = report["headline"]
    b = h["shown_labels"]
    assert (b["correct"], b["total"], b["status"]) == (29, 29, "insufficient data")
    assert b["passes"] is False and h["overall"]["passes"] is True and h["passes"] is False
    assert "INSUFFICIENT DATA" in render_markdown(report)


def test_b_counts_the_threshold_itself_as_shown_and_ignores_abstentions() -> None:
    gold, model = gold_set(100, conf=DISPLAY_CONFIDENCE_THRESHOLD)
    model[0] = model_record(1000, None)  # abstained: a miss in A, never shown
    del model[1]  # missing: a miss in A, never shown
    report = evaluate(gold, model)
    h = report["headline"]
    assert h["shown_labels"]["total"] == 98 and h["shown_labels"]["accuracy"] == 1.0
    assert (h["correct"], h["total"]) == (98, 100)
    rows = {r["title_key"]: r for r in report["per_title"]}
    assert rows["movie:1000"]["shown"] is False and rows["movie:1000"]["model_confidence"] is None
    assert rows["movie:1002"]["shown"] is True


# --- coverage --------------------------------------------------------------------------------


def test_coverage_reports_count_and_share_at_the_display_threshold() -> None:
    gold, model = gold_set(100, conf=HIDDEN)
    for rec in model[:40]:
        rec["layers"]["archetypal_plot"]["primary"]["confidence"] = SHOWN
    report = evaluate(gold, model)
    cov = report["headline"]["coverage"]
    assert (cov["at_display_threshold"], cov["at_display_threshold_share"]) == (40, 0.4)
    assert "At or above the display threshold: 40 titles (40.0% of scored)" in render_markdown(
        report
    )


def test_small_sample_cannot_pass() -> None:
    """QA: 4/5 used to pass."""
    gold, model = gold_set(5)
    h = evaluate(gold, model)["headline"]
    assert h["overall"]["passes"] is True
    assert h["coverage"]["passes"] is False and h["passes"] is False


def test_mismatched_titles_leave_the_denominator_and_are_listed() -> None:
    gold, model = gold_set(84, mismatched=4)
    report = evaluate(gold, model)
    h = report["headline"]
    assert h["total"] == 80 and h["correct"] == 80
    assert h["excluded_source_mismatch"] == [f"movie:{1000 + i}" for i in range(4)]
    assert report["counts"]["source_hash_mismatches"] == h["excluded_source_mismatch"]
    assert h["coverage"]["gold_annotated"] == 84
    assert h["coverage"]["at_display_threshold"] == 80
    assert h["passes"] is True  # 80 scored, 95.2% of the gold set, 80 shown and right
    assert all(r["title_key"] not in h["excluded_source_mismatch"] for r in report["per_title"])
    md = render_markdown(report)
    assert "excluded from every score" in md and "80 of 84 gold titles scored" in md


def test_coverage_share_passes_at_exactly_95_percent() -> None:
    """DECISIONS 2026-10-02: 95/100 passes (exact fractions, no float slop)."""
    gold, model = gold_set(100, mismatched=5)
    cov = evaluate(gold, model)["headline"]["coverage"]
    assert (cov["scored"], cov["gold_annotated"], cov["min_scored_fraction"]) == (95, 100, 0.95)
    assert cov["passes"] is True


def test_coverage_share_fails_below_95_percent() -> None:
    """94% passed the old 90% bar; it fails now."""
    gold, model = gold_set(100, mismatched=6)
    h = evaluate(gold, model)["headline"]
    assert h["total"] == 94 and h["accuracy"] == 1.0
    assert h["coverage"]["passes"] is False and h["passes"] is False


def test_too_many_mismatches_fail_coverage() -> None:
    gold, model = gold_set(100, mismatched=15)
    h = evaluate(gold, model)["headline"]
    assert h["accuracy"] == 1.0 and h["coverage"]["fraction"] == 0.85
    assert h["passes"] is False


# --- double labeling -------------------------------------------------------------------------


def test_index_gold_takes_two_labelers_and_refuses_more() -> None:
    g = gold_from_example("the_matrix", 1)
    idx = index_gold([g, second_label(g)])
    assert idx.reference["movie:1"] is g
    assert idx.second["movie:1"]["provenance"]["annotator"]["labeler_id"] == "L02"
    with pytest.raises(EvaluationInputError, match="more than two"):
        index_gold([g, second_label(g), second_label(g, labeler="L03")])
    with pytest.raises(EvaluationInputError, match="has two gold records"):
        index_gold([g, second_label(g, labeler="hari")])


def test_human_agreement_rule_and_disagreements() -> None:
    gold, model = gold_set(4)
    seconds = [
        second_label(gold[0]),  # agrees
        second_label(gold[1], primary="mystery"),  # disagrees
        second_label(gold[2], abstain=True),  # second abstains: a miss
    ]
    ref_abstained = gold_from_example("the_matrix", 1003, outcome="abstained")
    gold[3] = ref_abstained
    seconds.append(second_label(gold_from_example("the_matrix", 1003), labeler="L02"))
    report = evaluate(gold + seconds, model)
    hh = report["human_agreement"]
    assert hh["double_labeled_titles"] == 4
    assert (hh["correct"], hh["total"]) == (1, 3)  # reference abstention not scored
    by_key = {d["title_key"]: d for d in hh["disagreements"]}
    assert set(by_key) == {"movie:1001", "movie:1002", "movie:1003"}
    assert by_key["movie:1001"]["second"] == "mystery"
    assert by_key["movie:1002"]["second"] == "(abstained)"
    assert by_key["movie:1003"]["reference"] == "(abstained)"
    assert by_key["movie:1003"]["scored"] is False
    # the model is scored against the reference labeler only
    assert (report["headline"]["correct"], report["headline"]["total"]) == (3, 3)
    assert report["counts"]["gold_titles"] == 4
    md = render_markdown(report)
    assert "Disagreements for Hari to adjudicate" in md
    assert "| hari: overcoming_the_monster | L02: mystery | yes |" in md


def test_human_agreement_excludes_labelers_on_different_text() -> None:
    g = gold_from_example("the_matrix", 1)
    other = second_label(gold_from_example("the_matrix", 1, text=OTHER_TEXT))
    hh = evaluate([g, other], [model_record(1, "the_matrix")])["human_agreement"]
    assert hh["excluded_source_mismatch"] == ["movie:1"] and hh["total"] == 0
