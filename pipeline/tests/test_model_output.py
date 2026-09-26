"""Tests for the model-facing output schema and model-output <-> record conversion.

The limits checked here are the published structured-outputs limits (platform.claude.com,
"JSON Schema limitations" and "Schema complexity limits", read 2026-09-26).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from laminary_pipeline.annotation import validate_record
from laminary_pipeline.model_output import (
    ARC_KEYS,
    SUPPORTED_KEYWORDS,
    from_record,
    model_output_schema,
    to_record,
)

EXAMPLE_PATHS = sorted((Path(__file__).resolve().parent / "examples").glob("*.json"))
MAX_UNION_PROPERTIES = 16
MAX_OPTIONAL_PROPERTIES = 24


def schema_nodes(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every subschema in the model-facing schema (not property-name maps)."""
    yield node
    for sub in node.get("properties", {}).values():
        yield from schema_nodes(sub)
    if isinstance(node.get("items"), dict):
        yield from schema_nodes(node["items"])
    for sub in node.get("anyOf", []):
        yield from schema_nodes(sub)


def test_model_schema_is_valid_json_schema() -> None:
    Draft202012Validator.check_schema(model_output_schema())


def test_model_schema_uses_only_supported_keywords() -> None:
    for node in schema_nodes(model_output_schema()):
        assert set(node) <= SUPPORTED_KEYWORDS, set(node) - SUPPORTED_KEYWORDS
        if "minItems" in node:
            assert node["minItems"] in (0, 1)


def test_model_schema_objects_are_closed_and_fully_required() -> None:
    optional = 0
    for node in schema_nodes(model_output_schema()):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            optional += len(set(node["properties"]) - set(node["required"]))
    assert optional == 0 <= MAX_OPTIONAL_PROPERTIES


def test_model_schema_union_count_within_limit() -> None:
    unions = sum(
        1
        for node in schema_nodes(model_output_schema())
        if "anyOf" in node or isinstance(node.get("type"), list)
    )
    assert unions <= MAX_UNION_PROPERTIES
    assert unions == 2  # layers and beat_tags are nullable


def test_model_schema_has_no_emotional_arc_and_fixed_arc_keys() -> None:
    schema = model_output_schema()
    layers = schema["properties"]["layers"]["anyOf"][0]
    skel = layers["properties"]["structural_skeleton"]
    assert "emotional_arc" not in skel["properties"]
    assert skel["properties"]["arc_points"]["required"] == ARC_KEYS
    assert "arc_confidence" in skel["required"]
    assert "provenance" not in schema["properties"]


@pytest.mark.parametrize("path", EXAMPLE_PATHS, ids=lambda p: p.name)
def test_examples_round_trip_through_model_output(path: Path) -> None:
    record = json.loads(path.read_text(encoding="utf-8"))
    output = from_record(record)
    Draft202012Validator(model_output_schema()).validate(output)
    rebuilt = to_record(
        output,
        record_kind=record["record_kind"],
        title=record["title"],
        provenance=record["provenance"],
    )
    assert rebuilt == record
    assert validate_record(rebuilt) == []


def llm_provenance() -> dict[str, Any]:
    return {
        "annotated_at": "2026-09-26T00:00:00Z",
        "annotator": {"model_version": "model-x", "prompt_version": "p1", "run_id": "r1"},
        "sources": [
            {
                "kind": "wikipedia_plot",
                "ref": "https://en.wikipedia.org/wiki/Example",
                "revision": "1",
                "retrieved_at": "2026-09-26T00:00:00Z",
                "license": "CC-BY-SA-4.0",
                "word_count": 300,
                "content_sha256": "a" * 64,
            }
        ],
        "input_word_count": 300,
        "usage": {"input_tokens": 1200, "output_tokens": 900, "batch": True},
    }


def base_output() -> tuple[dict[str, Any], dict[str, Any]]:
    record = json.loads(EXAMPLE_PATHS[0].read_text(encoding="utf-8"))
    return from_record(record), record["title"]


def test_abstained_output_round_trip() -> None:
    _, title = base_output()
    output = {
        "outcome": "abstained",
        "abstain_reason": "not_a_narrative",
        "layers": None,
        "beat_tags": None,
    }
    Draft202012Validator(model_output_schema()).validate(output)
    record = to_record(
        output, record_kind="llm_annotation", title=title, provenance=llm_provenance()
    )
    assert validate_record(record) == []
    assert from_record(record) == output


def test_flat_arc_output_gets_capped_fallback_label() -> None:
    output, title = base_output()
    skel = output["layers"]["structural_skeleton"]
    skel["arc_points"] = dict.fromkeys(ARC_KEYS, 0.2)
    skel["arc_confidence"] = 0.9
    record = to_record(
        output, record_kind="llm_annotation", title=title, provenance=llm_provenance()
    )
    arc = record["layers"]["structural_skeleton"]["emotional_arc"]
    assert arc == {
        "label": "riches_to_rags",
        "confidence": 0.49,
        "method": "derived",
        "threshold_used": 0.3,
        "net_change_fallback": True,
    }
    assert validate_record(record) == []


def test_client_side_constraints_catch_what_the_grammar_cannot() -> None:
    """Values the model-facing schema allows but the stored record rejects."""
    output, title = base_output()
    output["beat_tags"]["tags"]["time_loop"]["confidence"] = 0.3  # < 0.5
    output["layers"]["surface_story"]["tones"] = ["warm", "warm"]
    Draft202012Validator(model_output_schema()).validate(output)
    record = to_record(
        output, record_kind="llm_annotation", title=title, provenance=llm_provenance()
    )
    problems = validate_record(record)
    assert any("time_loop/confidence" in p for p in problems)
    assert any("tones" in p for p in problems)


@pytest.mark.parametrize(
    "change",
    [
        {"layers": None},
        {"beat_tags": None},
        {"abstain_reason": "summary_too_thin"},
        {"outcome": "abstained", "abstain_reason": None},
    ],
    ids=["null layers", "null beat_tags", "reason on annotated", "abstained no reason"],
)
def test_inconsistent_outputs_raise(change: dict[str, Any]) -> None:
    output, title = base_output()
    output.update(change)
    with pytest.raises(ValueError):
        to_record(output, record_kind="llm_annotation", title=title, provenance=llm_provenance())


def test_out_of_range_arc_point_raises() -> None:
    output, title = base_output()
    output = copy.deepcopy(output)
    output["layers"]["structural_skeleton"]["arc_points"]["t05"] = 1.4
    with pytest.raises(ValueError):
        to_record(output, record_kind="llm_annotation", title=title, provenance=llm_provenance())
