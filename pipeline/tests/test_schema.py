"""Tests for the narrative annotation schema (pipeline/schema/annotation.schema.json).

Checks that:
- the schema is valid JSON Schema draft 2020-12;
- every example in pipeline/schema/examples/ validates and passes the semantic checks
  from docs/NARRATIVE_SCHEMA.md section 11;
- every controlled vocabulary in the schema matches the tables in docs/NARRATIVE_SCHEMA.md
  exactly (values and spoiler levels), and vice versa;
- all free text under layers and beat tags sits inside a safe_text or spoiler_text object.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

PIPELINE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PIPELINE_DIR.parent
SCHEMA_PATH = PIPELINE_DIR / "schema" / "annotation.schema.json"
EXAMPLES_DIR = PIPELINE_DIR / "schema" / "examples"
DOC_PATH = REPO_ROOT / "docs" / "NARRATIVE_SCHEMA.md"

ARC_POINT_COUNT = 11
MAJOR_MOVE = 0.3
ARC_LEGS = {
    ("up",): "rags_to_riches",
    ("down",): "riches_to_rags",
    ("down", "up"): "man_in_a_hole",
    ("up", "down"): "icarus",
    ("up", "down", "up"): "cinderella",
    ("down", "up", "down"): "oedipus",
}

EXAMPLE_PATHS = sorted(EXAMPLES_DIR.glob("*.json"))


# ---------- helpers ----------


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validator() -> Draft202012Validator:
    return Draft202012Validator(load_schema())


def load_example(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def schema_enums(schema: dict[str, Any]) -> dict[str, list[str]]:
    return {name: d["enum"] for name, d in schema["$defs"].items() if "enum" in d}


def resolve(schema: dict[str, Any], node: Any) -> Any:
    """Follow a local '#/$defs/<name>' $ref, if present."""
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        assert ref.startswith("#/$defs/"), f"unexpected non-local $ref {ref}"
        merged = {k: v for k, v in node.items() if k != "$ref"}
        target = schema["$defs"][ref.removeprefix("#/$defs/")]
        node = {**target, **merged} if merged else target
    return node


def ref_name(node: Any) -> str | None:
    if isinstance(node, dict) and isinstance(node.get("$ref"), str):
        return node["$ref"].removeprefix("#/$defs/")
    return None


def walk(schema: dict[str, Any], node: Any, path: tuple[str, ...]) -> Iterator[tuple]:
    """Yield (path, raw_node) for every subschema reachable under node, following $refs."""
    yield path, node
    node = resolve(schema, node)
    if not isinstance(node, dict):
        return
    for key, sub in node.get("properties", {}).items():
        yield from walk(schema, sub, (*path, key))
    if isinstance(node.get("additionalProperties"), dict):
        yield from walk(schema, node["additionalProperties"], (*path, "*"))
    if isinstance(node.get("propertyNames"), dict):
        yield from walk(schema, node["propertyNames"], (*path, "<key>"))
    if isinstance(node.get("items"), dict):
        yield from walk(schema, node["items"], (*path, "[]"))


def parse_doc_vocabularies() -> dict[str, list[list[str]]]:
    """Return {vocab_name: [row cells]} for each '<!-- vocab:NAME -->' table in the doc."""
    lines = DOC_PATH.read_text(encoding="utf-8").splitlines()
    vocabs: dict[str, list[list[str]]] = {}
    marker = re.compile(r"^<!-- vocab:([a-z_]+) -->$")
    i = 0
    while i < len(lines):
        m = marker.match(lines[i].strip())
        i += 1
        if not m:
            continue
        name = m.group(1)
        assert name not in vocabs, f"duplicate vocab marker {name}"
        rows: list[list[str]] = []
        # skip to the table, then read until it ends
        while i < len(lines) and not lines[i].startswith("|"):
            i += 1
        while i < len(lines) and lines[i].startswith("|"):
            cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            if cells[0].startswith("`"):
                rows.append(cells)
            i += 1
        assert rows, f"vocab table {name} has no rows"
        vocabs[name] = rows
    return vocabs


def arc_legs(points: list[float], threshold: float = MAJOR_MOVE) -> tuple[str, ...]:
    """Directions of major moves (>= threshold from the last peak/trough)."""
    legs: list[str] = []
    direction: str | None = None
    lo = hi = extreme = points[0]
    for p in points[1:]:
        if direction is None:
            lo, hi = min(lo, p), max(hi, p)
            if p - lo >= threshold:
                direction, extreme = "up", p
                legs.append("up")
            elif hi - p >= threshold:
                direction, extreme = "down", p
                legs.append("down")
        elif direction == "up":
            if p > extreme:
                extreme = p
            elif extreme - p >= threshold:
                direction, extreme = "down", p
                legs.append("down")
        else:
            if p < extreme:
                extreme = p
            elif p - extreme >= threshold:
                direction, extreme = "up", p
                legs.append("up")
    return tuple(legs)


def semantic_errors(record: dict[str, Any]) -> list[str]:
    """Checks from docs/NARRATIVE_SCHEMA.md section 11 that JSON Schema can't express."""
    errors: list[str] = []
    if record.get("outcome") != "annotated":
        return errors
    plot = record["layers"]["archetypal_plot"]
    if plot["primary"]["label"] in plot["secondary"]:
        errors.append("secondary repeats primary plot")
    beats = record["beat_tags"]
    extra = set(beats["spoiler_text"]["evidence"]) - set(beats["tags"])
    if extra:
        errors.append(f"evidence for tags not present: {sorted(extra)}")
    skel = record["layers"]["structural_skeleton"]
    implied = ARC_LEGS.get(arc_legs(skel["arc_points"]))
    if implied != skel["emotional_arc"]["label"]:
        errors.append(
            f"arc label {skel['emotional_arc']['label']} disagrees with arc_points ({implied})"
        )
    return errors


# ---------- schema and examples ----------


def test_schema_is_valid_draft_2020_12() -> None:
    schema = load_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)


def test_there_are_examples() -> None:
    assert len(EXAMPLE_PATHS) >= 2


@pytest.mark.parametrize("path", EXAMPLE_PATHS, ids=lambda p: p.name)
def test_example_validates(path: Path) -> None:
    record = load_example(path)
    errors = sorted(validator().iter_errors(record), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)
    assert record["record_kind"] == "illustrative_example", "examples must be marked illustrative"
    assert not semantic_errors(record)


@pytest.mark.parametrize(
    ("points", "label"),
    [
        ([-0.5, -0.4, -0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3, 0.4, 0.5], "rags_to_riches"),
        ([0.5, 0.4, 0.3, 0.2, 0.1, 0.0, -0.1, -0.2, -0.3, -0.4, -0.5], "riches_to_rags"),
        ([0.2, 0.0, -0.3, -0.6, -0.5, -0.2, 0.1, 0.3, 0.5, 0.6, 0.7], "man_in_a_hole"),
        ([-0.2, 0.1, 0.4, 0.6, 0.7, 0.5, 0.2, -0.1, -0.4, -0.6, -0.7], "icarus"),
        ([-0.5, -0.1, 0.3, 0.4, 0.0, -0.4, -0.3, 0.0, 0.4, 0.7, 0.8], "cinderella"),
        ([0.5, 0.1, -0.3, -0.4, 0.0, 0.4, 0.3, 0.0, -0.4, -0.7, -0.8], "oedipus"),
        # small wobbles (< 0.3) are ignored
        ([0.0, -0.2, -0.5, -0.4, -0.3, -0.4, -0.4, -0.5, -0.6, -0.5, 0.9], "man_in_a_hole"),
    ],
)
def test_arc_rule(points: list[float], label: str) -> None:
    assert ARC_LEGS[arc_legs(points)] == label


# ---------- negative cases: the schema must reject these ----------


def base_record() -> dict[str, Any]:
    return load_example(EXAMPLES_DIR / "groundhog_day.json")


def llm_record() -> dict[str, Any]:
    rec = base_record()
    rec["record_kind"] = "llm_annotation"
    rec["provenance"] = {
        "annotated_at": "2026-09-26T00:00:00Z",
        "annotator": {"model_version": "model-x", "prompt_version": "p1", "run_id": "r1"},
        "sources": [
            {
                "kind": "wikipedia_plot",
                "ref": "https://en.wikipedia.org/wiki/Example",
                "revision": "1",
                "retrieved_at": "2026-09-26T00:00:00Z",
                "license": "CC-BY-SA-4.0",
                "word_count": 400,
                "content_sha256": "0" * 64,
            }
        ],
        "input_word_count": 400,
        "usage": {"input_tokens": 1, "output_tokens": 1, "batch": True},
    }
    return rec


def abstained_record() -> dict[str, Any]:
    rec = llm_record()
    del rec["layers"], rec["beat_tags"]
    rec["outcome"] = "abstained"
    rec["abstain_reason"] = "summary_too_thin"
    return rec


def test_llm_and_abstained_records_validate() -> None:
    v = validator()
    for rec in (llm_record(), abstained_record()):
        assert not list(v.iter_errors(rec))


DELETE = object()
SKEL = "layers/structural_skeleton"

# (case, base, slash-separated path, new value or DELETE). "base" is "llm" or "abstained".
REJECT_CASES: list[tuple[str, str, str, Any]] = [
    ("llm without model_version", "llm", "provenance/annotator/model_version", DELETE),
    ("llm without sources", "llm", "provenance/sources", []),
    ("llm with null tmdb_id", "llm", "title/tmdb_id", None),
    ("10 arc points", "llm", f"{SKEL}/arc_points", [0.0] * 10),
    ("arc point out of range", "llm", f"{SKEL}/arc_points", [1.5] + [0.0] * 10),
    ("unknown arc label", "llm", f"{SKEL}/emotional_arc/label", "w_shape"),
    ("unknown beat tag", "llm", "beat_tags/tags/chosen_one", 0.9),
    ("confidence above 1", "llm", "beat_tags/tags/time_loop", 1.2),
    (
        "three secondary plots",
        "llm",
        "layers/archetypal_plot/secondary",
        {"comedy": 0.6, "the_quest": 0.6, "tragedy": 0.6},
    ),
    ("text outside safe/spoiler", "llm", "layers/surface_story/note", "x"),
    ("spoiler key in safe_text", "llm", "layers/surface_story/safe_text/resolution", "x"),
    ("movie with series_status", "llm", "title/series_status", "ended"),
    ("tv without series_status", "llm", "title/media_type", "tv_series"),
    ("wrong schema_version", "llm", "schema_version", "9.9.9"),
    ("annotated with abstain_reason", "llm", "abstain_reason", "summary_too_thin"),
    ("abstained without reason", "abstained", "abstain_reason", DELETE),
    ("abstained with beat tags", "abstained", "beat_tags", "<from base>"),
    ("abstained with layers", "abstained", "layers", "<from base>"),
]


def mutate(base: str, path: str, value: Any) -> dict[str, Any]:
    rec = llm_record() if base == "llm" else abstained_record()
    *parents, last = path.split("/")
    node = rec
    for key in parents:
        node = node[key]
    if value == "<from base>":  # a complete, valid block copied from the base example
        value = base_record()[last]
    if value is DELETE:
        del node[last]
    else:
        node[last] = value
    return rec


@pytest.mark.parametrize(
    ("case", "base", "path", "value"), REJECT_CASES, ids=[c[0] for c in REJECT_CASES]
)
def test_schema_rejects(case: str, base: str, path: str, value: Any) -> None:
    rec = mutate(base, path, value)
    assert list(validator().iter_errors(rec)), f"schema accepted: {case}"


def test_semantic_checks_catch_errors() -> None:
    rec = copy.deepcopy(base_record())
    rec["layers"]["archetypal_plot"]["secondary"] = {"rebirth": 0.6}
    rec["beat_tags"]["spoiler_text"]["evidence"]["found_family"] = "x"
    rec["layers"]["structural_skeleton"]["emotional_arc"]["label"] = "icarus"
    assert len(semantic_errors(rec)) == 3


# ---------- vocabulary: schema <-> doc, mechanically ----------


def test_all_enums_are_named_defs() -> None:
    """Every enum lives directly in $defs, so the doc can name it."""
    schema = load_schema()

    def find(node: Any, path: str) -> Iterator[str]:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "enum" and not re.fullmatch(r"/\$defs/[a-z_]+", path):
                    yield path
                yield from find(v, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                yield from find(v, f"{path}/{i}")

    assert list(find(schema, "")) == []


def test_doc_vocabularies_match_schema_enums() -> None:
    enums = schema_enums(load_schema())
    doc = parse_doc_vocabularies()
    assert set(doc) == set(enums), (
        f"only in doc: {sorted(set(doc) - set(enums))}; "
        f"only in schema: {sorted(set(enums) - set(doc))}"
    )
    for name, values in enums.items():
        assert len(values) == len(set(values)), f"duplicate enum values in {name}"
        doc_values = [row[0].strip("`") for row in doc[name]]
        assert len(doc_values) == len(set(doc_values)), f"duplicate doc rows in {name}"
        assert set(doc_values) == set(values), (
            f"{name}: only in doc {sorted(set(doc_values) - set(values))}, "
            f"only in schema {sorted(set(values) - set(doc_values))}"
        )


def test_every_doc_term_has_a_definition() -> None:
    for name, rows in parse_doc_vocabularies().items():
        for row in rows:
            assert len(row) in (3, 4), f"{name}: malformed row {row}"
            assert len(row[-1]) >= 20, f"{name}: {row[0]} has no real definition"


def test_spoiler_levels_cover_exactly_the_narrative_vocabularies() -> None:
    """Every enum a viewer can see (under layers or beat_tags) has per-term spoiler levels."""
    schema = load_schema()
    shown: set[str] = set()
    for root in ("layers", "beat_tags_block"):
        for _, raw in walk(schema, {"$ref": f"#/$defs/{root}"}, (root,)):
            name = ref_name(raw)
            if name and "enum" in schema["$defs"][name]:
                shown.add(name)
    levels = schema["x-laminary-spoiler-levels"]["vocabularies"]
    assert set(levels) == shown
    allowed = set(schema["$defs"]["spoiler_level"]["enum"])
    for name, mapping in levels.items():
        assert set(mapping) == set(schema["$defs"][name]["enum"]), name
        assert set(mapping.values()) <= allowed, name


def test_doc_spoiler_column_matches_schema() -> None:
    levels = load_schema()["x-laminary-spoiler-levels"]["vocabularies"]
    for name, rows in parse_doc_vocabularies().items():
        if name in levels:
            for row in rows:
                assert len(row) == 4, f"{name}: row needs a spoiler column: {row}"
                assert row[2] == levels[name][row[0].strip("`")], f"{name}: {row[0]}"
        else:
            assert all(len(row) == 3 for row in rows), f"{name}: unexpected spoiler column"


def test_field_spoiler_levels_point_at_real_fields() -> None:
    schema = load_schema()
    paths = {".".join(p) for p, _ in walk(schema, {"$ref": "#/$defs/layers"}, ("layers",))}
    for field in schema["x-laminary-spoiler-levels"]["fields"]:
        assert field in paths, field


# ---------- spoiler separation ----------


def test_all_free_text_is_inside_safe_or_spoiler_text() -> None:
    schema = load_schema()
    text_paths: list[tuple[str, ...]] = []
    for root in ("layers", "beat_tags_block"):
        for path, raw in walk(schema, {"$ref": f"#/$defs/{root}"}, (root,)):
            node = resolve(schema, raw)
            if isinstance(node, dict) and node.get("type") == "string" and path[-1] != "<key>":
                text_paths.append(path)
    assert text_paths, "expected some free-text fields"
    bad = [p for p in text_paths if "safe_text" not in p and "spoiler_text" not in p]
    assert not bad, f"free text outside safe_text/spoiler_text: {bad}"
