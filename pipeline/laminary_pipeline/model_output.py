"""The model-facing output schema, and conversion between model output and stored records.

The stored record schema (annotation.schema.json) uses JSON Schema features that Claude's
structured outputs do not accept. ``model_output_schema()`` derives the schema sent to the API:

- every ``$ref`` is inlined (no ``$ref``/``$defs``);
- unsupported constraints are stripped and enforced client-side instead: numeric ranges
  (``minimum``/``maximum``), string lengths, ``maxItems``/``uniqueItems``, ``pattern``,
  ``if``/``then``, and the semantic checks in ``annotation.semantic_errors``;
- provenance and title are not in it (the pipeline fills them);
- the model writes ``arc_points`` as an object with 11 fixed keys (t00 ... t10), so the grammar
  enforces the count, plus ``arc_confidence``; the pipeline derives ``emotional_arc``
  (laminary_pipeline.arc);
- ``layers``/``beat_tags`` are nullable (null when abstaining) and ``abstain_reason`` takes
  null when annotating, so every property can be required.

Limits checked in tests against the published structured-outputs limits: no optional
properties, at most 16 union-typed properties, ``minItems`` only 0 or 1.
"""

from __future__ import annotations

import copy
from typing import Any

from laminary_pipeline.annotation import load_schema, schema_version
from laminary_pipeline.arc import ARC_POINT_COUNT, derive_arc, derived_confidence

ARC_KEYS = [f"t{i:02d}" for i in range(ARC_POINT_COUNT)]

# Keywords kept in the model-facing schema. Everything else is dropped (and enforced client-side).
SUPPORTED_KEYWORDS = {
    "type", "properties", "required", "additionalProperties", "items", "enum", "const",
    "anyOf", "description", "minItems",
}


def _inline(node: Any, defs: dict[str, Any]) -> Any:
    """Resolve local $refs recursively and drop unsupported keywords."""
    if isinstance(node, list):
        return [_inline(n, defs) for n in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        target = _inline(defs[node["$ref"].removeprefix("#/$defs/")], defs)
        extra = {k: v for k, v in node.items() if k != "$ref"}
        return {**target, **_inline(extra, defs)}
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key not in SUPPORTED_KEYWORDS:
            continue
        if key == "properties":
            out[key] = {name: _inline(sub, defs) for name, sub in value.items()}
        elif key == "minItems" and value not in (0, 1):
            continue
        else:
            out[key] = _inline(value, defs)
    return out


def model_output_schema() -> dict[str, Any]:
    """JSON schema for the model's output (structured outputs / strict tool input)."""
    defs = load_schema()["$defs"]
    layers = _inline({"$ref": "#/$defs/layers"}, defs)
    skel = layers["properties"]["structural_skeleton"]
    skel["properties"] = {
        "arc_points": {
            "description": (
                "Protagonist fortune in [-1, 1] at t = 0.0 (t00) through 1.0 (t10), "
                "in presentation order."
            ),
            "type": "object",
            "additionalProperties": False,
            "required": ARC_KEYS,
            "properties": {k: {"type": "number"} for k in ARC_KEYS},
        },
        "arc_confidence": {
            "description": "Confidence in [0, 1] that the arc points capture the story's shape.",
            "type": "number",
        },
        **{k: v for k, v in skel["properties"].items() if k not in ("emotional_arc", "arc_points")},
    }
    skel["required"] = list(skel["properties"])
    beats = _inline({"$ref": "#/$defs/beat_tags_block"}, defs)
    reasons = defs["abstain_reason"]["enum"]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["outcome", "abstain_reason", "layers", "beat_tags"],
        "properties": {
            "outcome": {"enum": defs["outcome"]["enum"]},
            "abstain_reason": {
                "description": "Null when outcome is 'annotated'.",
                "enum": [*reasons, None],
            },
            "layers": {"anyOf": [layers, {"type": "null"}]},
            "beat_tags": {"anyOf": [beats, {"type": "null"}]},
        },
    }


def to_record(
    output: dict[str, Any],
    *,
    record_kind: str,
    title: dict[str, Any],
    provenance: dict[str, Any],
) -> dict[str, Any]:
    """Build a stored record from one model output. Validate the result with
    ``annotation.validate_record`` before storing it.

    Raises ValueError when the output is internally inconsistent in a way that can't be
    expressed as a record (outcome vs null layers, arc points out of range).
    """
    record: dict[str, Any] = {
        "schema_version": schema_version(),
        "record_kind": record_kind,
        "title": copy.deepcopy(title),
        "provenance": copy.deepcopy(provenance),
        "outcome": output["outcome"],
    }
    if output["outcome"] == "abstained":
        if output.get("abstain_reason") is None:
            raise ValueError("abstained output without abstain_reason")
        record["abstain_reason"] = output["abstain_reason"]
        return record
    if output.get("layers") is None or output.get("beat_tags") is None:
        raise ValueError("annotated output with null layers or beat_tags")
    if output.get("abstain_reason") is not None:
        raise ValueError("annotated output with an abstain_reason")

    layers = copy.deepcopy(output["layers"])
    skel = layers["structural_skeleton"]
    points = [skel["arc_points"][k] for k in ARC_KEYS]
    derivation = derive_arc(points)
    layers["structural_skeleton"] = {
        "emotional_arc": {
            "label": derivation.label,
            "confidence": derived_confidence(skel["arc_confidence"], derivation),
            "method": "derived",
            "threshold_used": derivation.threshold_used,
            "net_change_fallback": derivation.net_change_fallback,
        },
        "arc_points": points,
        "chronology": skel["chronology"],
        "safe_text": skel["safe_text"],
        "spoiler_text": skel["spoiler_text"],
    }
    record["layers"] = layers
    record["beat_tags"] = copy.deepcopy(output["beat_tags"])
    return record


def from_record(record: dict[str, Any]) -> dict[str, Any]:
    """Inverse of ``to_record`` for a full (non-gold) record. Used for tests and prompt examples."""
    if record["outcome"] == "abstained":
        return {
            "outcome": "abstained",
            "abstain_reason": record["abstain_reason"],
            "layers": None,
            "beat_tags": None,
        }
    layers = copy.deepcopy(record["layers"])
    skel = layers["structural_skeleton"]
    arc = skel.pop("emotional_arc")
    layers["structural_skeleton"] = {
        "arc_points": dict(zip(ARC_KEYS, skel["arc_points"], strict=True)),
        "arc_confidence": arc["confidence"],
        "chronology": skel["chronology"],
        "safe_text": skel["safe_text"],
        "spoiler_text": skel["spoiler_text"],
    }
    return {
        "outcome": "annotated",
        "abstain_reason": None,
        "layers": layers,
        "beat_tags": copy.deepcopy(record["beat_tags"]),
    }
