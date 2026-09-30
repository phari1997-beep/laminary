"""Evaluation maths on tiny hand-checkable fixtures (NARRATIVE_SCHEMA section 14)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from annotate_support import STORY, gold_from_example, source_meta, valid_output

from laminary_pipeline.annotation import validate_record
from laminary_pipeline.evaluate import metrics as m
from laminary_pipeline.evaluate.__main__ import main as eval_main
from laminary_pipeline.evaluate.pairs import (
    NOT_RUN,
    PairExpectation,
    check_pairs,
    load_pairs,
)
from laminary_pipeline.evaluate.pairs import (
    render_markdown as render_pairs,
)
from laminary_pipeline.evaluate.report import (
    EvaluationInputError,
    evaluate,
    load_records,
    render_markdown,
)
from laminary_pipeline.model_output import to_record

MODEL = "claude-opus-5-5"


def model_record(tmdb: int, example: str | None, *, prompt="annotate-1.0.0") -> dict:
    output = (
        valid_output(example)
        if example
        else {
            "outcome": "abstained",
            "abstain_reason": "summary_too_thin",
            "layers": None,
            "beat_tags": None,
        }
    )
    rec = to_record(
        output,
        record_kind="llm_annotation",
        title={
            "media_type": "movie",
            "name": f"Gold {tmdb}",
            "release_year": 2019,
            "tmdb_id": tmdb,
            "wikidata_id": None,
        },
        provenance={
            "annotated_at": "2026-09-30T00:00:00Z",
            "annotator": {"model_version": MODEL, "prompt_version": prompt, "run_id": "run1"},
            "sources": [source_meta()],
            "input_word_count": source_meta()["word_count"],
            "usage": {
                "input_tokens": 1_000_000,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "batch": True,
            },
        },
    )
    assert validate_record(rec) == [], validate_record(rec)
    return rec


@pytest.fixture
def fixture_records():
    fallback_gold = gold_from_example(
        "the_matrix", 104, primary="rebellion_against_the_one", arc_label="riches_to_rags"
    )
    fallback_gold["layers"]["structural_skeleton"]["emotional_arc"]["net_change_fallback"] = True
    gold = [
        gold_from_example("the_matrix", 101),
        gold_from_example("groundhog_day", 102),
        gold_from_example("breaking_bad", 103),
        fallback_gold,
        gold_from_example("the_matrix", 105),
        gold_from_example("the_matrix", 106),
        gold_from_example("the_matrix", 107, outcome="abstained"),
    ]
    for g in gold:
        assert validate_record(g) == [], validate_record(g)
    model = [
        model_record(101, "the_matrix"),
        model_record(102, "groundhog_day"),
        model_record(103, "the_matrix"),
        model_record(104, "the_matrix"),
        model_record(105, None),
        model_record(107, None),
    ]
    return gold, model


def test_headline_counts_abstentions_and_missing_as_misses(fixture_records) -> None:
    report = evaluate(*fixture_records)
    h = report["headline"]
    assert (h["correct"], h["total"]) == (2, 6)
    assert h["accuracy"] == pytest.approx(2 / 6) and h["passes"] is False
    assert h["model_abstained_counted_as_miss"] == 1
    assert h["no_model_record_counted_as_miss"] == 1
    assert report["counts"]["gold_abstained"] == 1


def test_primary_kappa_lenient_and_blueprint(fixture_records) -> None:
    report = evaluate(*fixture_records)
    # gold: otm x3, rebirth, tragedy, rebellion; model: otm x3, rebirth, abstained, missing
    assert report["primary_plot"]["kappa"] == pytest.approx((2 / 6 - 10 / 36) / (1 - 10 / 36))
    assert report["primary_plot"]["lenient"]["correct"] == 3
    assert report["blueprint"] == {"correct": 3, "total": 4, "accuracy": 0.75}


def test_emotional_arc_breaks_out_flagged_titles(fixture_records) -> None:
    arc = evaluate(*fixture_records)["emotional_arc"]
    assert arc["headline_unflagged"] == {"correct": 2, "total": 3, "accuracy": pytest.approx(2 / 3)}
    assert arc["fallback"]["count"] == 1 and arc["fallback"]["correct"] == 0
    assert arc["reduced_shape"]["count"] == 0
    assert arc["not_scored_model_abstained_or_missing"] == 2


def test_presence_prf_and_calibration(fixture_records) -> None:
    report = evaluate(*fixture_records)
    rebirth = report["presence"]["plots"]["rebirth"]
    assert (rebirth["tp"], rebirth["fp"], rebirth["fn"], rebirth["tn"]) == (3, 1, 0, 0)
    assert rebirth["precision"] == 0.75 and rebirth["recall"] == 1.0
    cal = report["calibration"]["primary"]
    assert cal["0.80-0.89"] == {"n": 3, "accuracy": pytest.approx(1 / 3)}
    assert cal["0.90-1.00"] == {"n": 1, "accuracy": 1.0}


def test_abstention_flat_rates_and_leaks(fixture_records) -> None:
    flags = [
        {"title_key": "movie:101", "field": "x", "grade": "major"},
        {"title_key": "movie:102", "field": "x", "grade": "mild"},
        {"title_key": "movie:103", "field": "x", "grade": "none"},
    ]
    report = evaluate(*fixture_records, leak_flags=flags)
    ab = report["abstention"]
    assert ab["rate"] == pytest.approx(2 / 6) and ab["on_titles_gold_could_label"] == ["movie:105"]
    assert ab["model_also_abstained_on_those"] == 1
    flat = report["flat_and_reduced_arcs"]["gold_set_gold"]["movie"]
    assert flat["titles"] == 6 and flat["net_change_fallback_rate"] == pytest.approx(1 / 6)
    leaks = report["spoiler_leaks"]
    assert leaks["major_leaks"] == 1 and leaks["passes_major_target"] is False
    assert leaks["mild_rate"] == pytest.approx(1 / 3)
    assert evaluate(*fixture_records)["spoiler_leaks"]["status"].startswith("not measured")


def test_cost_from_records(fixture_records) -> None:
    cost = evaluate(*fixture_records)["cost"]
    # 6 records x 1M input tokens x $4/MTok x 0.5 batch
    assert cost["usd_total"] == pytest.approx(12.0)
    assert "excludes cache writes" in cost["source"]


def test_source_mismatch_is_flagged(fixture_records) -> None:
    gold, model = fixture_records
    other = STORY.replace("lighthouse", "beacon tower")
    gold[1] = gold_from_example("groundhog_day", 102, text=other)
    assert evaluate(gold, model)["counts"]["source_hash_mismatches"] == ["movie:102"]


def test_mixed_prompt_versions_refused(fixture_records) -> None:
    gold, model = fixture_records
    model[0] = model_record(101, "the_matrix", prompt="annotate-9.9.9")
    with pytest.raises(EvaluationInputError, match="one \\(schema, prompt, model\\) group"):
        evaluate(gold, model)


def test_markdown_and_cli(fixture_records, tmp_path: Path) -> None:
    gold, model = fixture_records
    md = render_markdown(evaluate(gold, model))
    assert "NOT MET" in md and "2/6 = 33.3%" in md
    (tmp_path / "gold.jsonl").write_text("\n".join(json.dumps(g) for g in gold))
    run = tmp_path / "annotations" / "run1"
    run.mkdir(parents=True)
    (run / "annotations.jsonl").write_text("\n".join(json.dumps(r) for r in model))
    (run / "cost.json").write_text(
        json.dumps({"usd_total": 1.5, "titles_attempted": 6, "records_stored": 6})
    )
    pairs = tmp_path / "pairs.csv"
    pairs.write_text(
        "title_a,title_b,expectation,note\nmovie:12,movie:8681,should_not_match,dad finds kid\n"
    )
    assert (
        eval_main(
            [
                "--gold",
                str(tmp_path / "gold.jsonl"),
                "--run",
                "run1",
                "--annotations-dir",
                str(tmp_path / "annotations"),
                "--pairs",
                str(pairs),
            ]
        )
        == 0
    )
    report = json.loads((run / "evaluation.json").read_text())
    assert report["cost"]["usd_per_title_attempted"] == 0.25
    assert report["pairs"]["status"] == NOT_RUN
    assert "movie:8681" in (run / "evaluation.md").read_text()


def test_invalid_records_stop_the_evaluation(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema_version": "1.0.0"}))
    with pytest.raises(EvaluationInputError):
        load_records([bad])


# --- metrics -------------------------------------------------------------------------------


def test_kappa_known_value_and_edge_cases() -> None:
    assert m.cohen_kappa([1, 1, 0, 0], [1, 0, 0, 0]) == pytest.approx(0.5)
    assert m.cohen_kappa([], []) is None
    assert m.cohen_kappa(["a", "a"], ["a", "a"]) is None
    with pytest.raises(ValueError):
        m.cohen_kappa([1], [1, 2])


def test_confusion_and_bands() -> None:
    c = m.Confusion()
    for gold, pred in [(True, True), (True, False), (False, True), (False, False)]:
        c.add(gold, pred)
    s = c.summary()
    assert (s["precision"], s["recall"], s["f1"], s["accuracy"]) == (0.5, 0.5, 0.5, 0.5)
    assert m.Confusion().summary()["f1"] is None
    assert [m.band(x) for x in (1.0, 0.9, 0.85, 0.8, 0.75, 0.5, 0.49)] == [
        "0.90-1.00",
        "0.90-1.00",
        "0.80-0.89",
        "0.80-0.89",
        "0.70-0.79",
        "0.50-0.69",
        "<0.50",
    ]
    assert m.jaccard(["a", "b"], ["b", "c"]) == pytest.approx(1 / 3)


# --- pairs stub ----------------------------------------------------------------------------


def test_pairs_stub_lists_pairs_unscored_and_scores_with_a_similarity(tmp_path: Path) -> None:
    pairs = [
        PairExpectation("movie:12", "movie:8681", "should_not_match", "Nemo / Taken"),
        PairExpectation("movie:1", "movie:2", "should_match"),
    ]
    report = check_pairs(pairs)
    assert report.status == NOT_RUN and all(r.ok is None for r in report.results)
    sims = {("movie:12", "movie:8681"): 0.9, ("movie:1", "movie:2"): 0.9}
    scored = check_pairs(pairs, lambda a, b: sims[(a, b)], threshold=0.8)
    assert [r.ok for r in scored.results] == [False, True]
    assert "VIOLATION" in render_pairs(scored)
    bad = tmp_path / "p.csv"
    bad.write_text("title_a,title_b,expectation\na,b,maybe\n")
    with pytest.raises(ValueError, match="expectation"):
        load_pairs(bad)
