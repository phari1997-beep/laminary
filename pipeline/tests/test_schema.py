"""Tests for the stored annotation schema and its documentation.

Checks that:
- the packaged schema loads via importlib.resources and is valid JSON Schema draft 2020-12;
- every example in tests/examples/ passes schema and semantic validation;
- the schema rejects known-bad records for the intended reason, and the semantic checks
  catch what JSON Schema can't;
- docs/NARRATIVE_SCHEMA.md matches the schema mechanically: vocabulary tables, spoiler levels,
  and every backticked identifier;
- all free text under layers and beat tags sits inside a safe_text or spoiler_text object.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from laminary_pipeline.annotation import (
    SCHEMA_RESOURCE,
    load_schema,
    semantic_errors,
    validate_record,
    validator,
)
from laminary_pipeline.model_output import model_output_schema

TESTS_DIR = Path(__file__).resolve().parent
EXAMPLES_DIR = TESTS_DIR / "examples"
DOC_PATH = TESTS_DIR.parents[1] / "docs" / "NARRATIVE_SCHEMA.md"
EXAMPLE_PATHS = sorted(EXAMPLES_DIR.glob("*.json"))
LEVEL_ORDER = {"none": 0, "mild": 1, "major": 2}


# ---------- helpers ----------


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def example(name: str) -> dict[str, Any]:
    return load_json(EXAMPLES_DIR / f"{name}.json")


def resolve(schema: dict[str, Any], node: Any) -> Any:
    """Follow local '#/$defs/<name>' $refs, merging sibling keywords."""
    while isinstance(node, dict) and "$ref" in node:
        target = schema["$defs"][node["$ref"].removeprefix("#/$defs/")]
        siblings = {k: v for k, v in node.items() if k != "$ref"}
        node = {**target, **siblings}
    return node


def ref_name(node: Any) -> str | None:
    if isinstance(node, dict) and isinstance(node.get("$ref"), str):
        return node["$ref"].removeprefix("#/$defs/")
    return None


def walk(schema: dict[str, Any], node: Any, path: tuple[str, ...]) -> Iterator[tuple]:
    """Yield (path, raw_node) for every subschema under node, following $refs."""
    yield path, node
    node = resolve(schema, node)
    if not isinstance(node, dict):
        return
    for key, sub in node.get("properties", {}).items():
        yield from walk(schema, sub, (*path, key))
    for key in ("additionalProperties", "items"):
        if isinstance(node.get(key), dict):
            yield from walk(schema, node[key], (*path, "[]"))


def viewer_roots() -> list[tuple[str, str]]:
    return [
        ("layers", "layers"),
        ("layers", "gold_layers"),
        ("beat_tags", "beat_tags_block"),
        ("beat_tags", "gold_beat_tags"),
    ]


def is_string_type(node: dict[str, Any]) -> bool:
    t = node.get("type")
    return t == "string" or (isinstance(t, list) and "string" in t)


TABLE_ROW = re.compile(r"^`[A-Za-z0-9_.-]+`$")
SEPARATOR = re.compile(r"^\|(\s*:?-+:?\s*\|)+$")


def parse_vocab_tables(text: str) -> dict[str, list[list[str]]]:
    """{vocab: rows} for each '<!-- vocab:NAME -->' table. Strict: after the header and
    separator, every row must be a vocabulary row whose first cell is one backticked term."""
    lines = text.splitlines()
    marker = re.compile(r"^<!-- vocab:([a-z_]+) -->$")
    vocabs: dict[str, list[list[str]]] = {}
    i = 0
    while i < len(lines):
        m = marker.match(lines[i].strip())
        i += 1
        if not m:
            continue
        name = m.group(1)
        if name in vocabs:
            raise ValueError(f"duplicate vocab marker {name}")
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i + 1 >= len(lines) or not lines[i].startswith("|"):
            raise ValueError(f"{name}: marker not followed by a table")
        if not SEPARATOR.match(lines[i + 1].strip()):
            raise ValueError(f"{name}: second table line is not a separator")
        i += 2
        rows: list[list[str]] = []
        while i < len(lines) and lines[i].startswith("|"):
            cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            if not TABLE_ROW.match(cells[0]):
                raise ValueError(f"{name}: non-vocabulary row in table: {lines[i]!r}")
            rows.append(cells)
            i += 1
        if not rows:
            raise ValueError(f"vocab table {name} has no rows")
        vocabs[name] = rows
    return vocabs


def doc_text() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def schema_enums() -> dict[str, list[Any]]:
    return {n: d["enum"] for n, d in load_schema()["$defs"].items() if "enum" in d}


# ---------- schema and examples ----------


def test_schema_ships_as_package_data() -> None:
    package, path = SCHEMA_RESOURCE
    assert resources.files(package).joinpath(path).is_file()


def test_schema_is_valid_draft_2020_12() -> None:
    schema = load_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)
    assert schema["properties"]["schema_version"]["const"].startswith("0.")


def test_there_are_examples() -> None:
    assert len(EXAMPLE_PATHS) >= 2


@pytest.mark.parametrize("path", EXAMPLE_PATHS, ids=lambda p: p.name)
def test_example_validates(path: Path) -> None:
    record = load_json(path)
    assert validate_record(record) == []
    assert record["record_kind"] == "illustrative_example", "examples must be marked illustrative"


# ---------- records built for negative tests ----------


def llm_record() -> dict[str, Any]:
    rec = example("groundhog_day")
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


def gold_record() -> dict[str, Any]:
    """A minimal gold label: only the scored labels."""
    full = llm_record()
    rec = {k: full[k] for k in ("schema_version", "title", "outcome")}
    rec["record_kind"] = "gold_label"
    rec["provenance"] = copy.deepcopy(full["provenance"])
    del rec["provenance"]["usage"]
    rec["provenance"]["annotator"] = {"labeler_id": "hari", "guide_version": "g1"}
    layers = full["layers"]
    rec["layers"] = {
        "archetypal_plot": {
            "primary": {"label": layers["archetypal_plot"]["primary"]["label"]},
            "plots": {
                k: {"present": v["present"]} for k, v in layers["archetypal_plot"]["plots"].items()
            },
        },
        "mythic_blueprint": {
            "blueprint": {"label": "heros_journey"},
            "stages": {
                k: {"present": v["present"]}
                for k, v in layers["mythic_blueprint"]["stages"].items()
            },
        },
        "structural_skeleton": {
            "emotional_arc": {"label": "man_in_a_hole", "method": "labeler_assigned"},
        },
    }
    rec["beat_tags"] = {
        "tags": {k: {"present": v["present"]} for k, v in full["beat_tags"]["tags"].items()}
    }
    return rec


BASES = {"llm": llm_record, "abstained": abstained_record, "gold": gold_record}
DELETE = object()
FROM_BASE = object()
SKEL = "layers/structural_skeleton"

# (case, base, slash path, new value | DELETE | FROM_BASE, expected keyword, expected error path)
REJECT_CASES: list[tuple[str, str, str, Any, str | None, str]] = [
    (
        "llm without model_version",
        "llm",
        "provenance/annotator/model_version",
        DELETE,
        "required",
        "provenance/annotator",
    ),
    ("llm without usage", "llm", "provenance/usage", DELETE, "required", "provenance"),
    ("llm without sources", "llm", "provenance/sources", [], "minItems", "provenance/sources"),
    (
        "llm below 150 words",
        "llm",
        "provenance/input_word_count",
        149,
        "minimum",
        "provenance/input_word_count",
    ),
    ("llm with null tmdb_id", "llm", "title/tmdb_id", None, "type", "title/tmdb_id"),
    (
        "llm with labeler-assigned arc",
        "llm",
        f"{SKEL}/emotional_arc/method",
        "labeler_assigned",
        "const",
        f"{SKEL}/emotional_arc/method",
    ),
    (
        "derived arc without threshold",
        "llm",
        f"{SKEL}/emotional_arc/threshold_used",
        DELETE,
        "required",
        f"{SKEL}/emotional_arc",
    ),
    (
        "bad annotated_at format",
        "llm",
        "provenance/annotated_at",
        "2026-09-26",
        "format",
        "provenance/annotated_at",
    ),
    (
        "annotated_at without offset",
        "llm",
        "provenance/annotated_at",
        "2026-09-26T00:00:00",
        "format",
        "provenance/annotated_at",
    ),
    (
        "bad retrieved_at format",
        "llm",
        "provenance/sources/0/retrieved_at",
        "yesterday",
        "format",
        "provenance/sources/0/retrieved_at",
    ),
    ("10 arc points", "llm", f"{SKEL}/arc_points", [0.0] * 10, "minItems", f"{SKEL}/arc_points"),
    (
        "arc point out of range",
        "llm",
        f"{SKEL}/arc_points",
        [1.5] + [0.0] * 10,
        "maximum",
        f"{SKEL}/arc_points/0",
    ),
    (
        "unknown arc label",
        "llm",
        f"{SKEL}/emotional_arc/label",
        "w_shape",
        "enum",
        f"{SKEL}/emotional_arc/label",
    ),
    (
        "unknown beat tag",
        "llm",
        "beat_tags/tags/chosen_one",
        {"present": True, "confidence": 0.9},
        "additionalProperties",
        "beat_tags/tags",
    ),
    (
        "missing beat tag judgment",
        "llm",
        "beat_tags/tags/time_loop",
        DELETE,
        "required",
        "beat_tags/tags",
    ),
    (
        "judgment confidence below 0.5",
        "llm",
        "beat_tags/tags/found_family/confidence",
        0.4,
        "minimum",
        "beat_tags/tags/found_family/confidence",
    ),
    (
        "judgment confidence above 1",
        "llm",
        "beat_tags/tags/time_loop/confidence",
        1.2,
        "maximum",
        "beat_tags/tags/time_loop/confidence",
    ),
    (
        "stage judgment without confidence",
        "llm",
        "layers/mythic_blueprint/stages/ordeal/confidence",
        DELETE,
        "required",
        "layers/mythic_blueprint/stages/ordeal",
    ),
    (
        "four tones",
        "llm",
        "layers/surface_story/tones",
        ["tense", "warm", "eerie", "bleak"],
        "maxItems",
        "layers/surface_story/tones",
    ),
    (
        "repeated tone",
        "llm",
        "layers/surface_story/tones",
        ["warm", "warm"],
        "uniqueItems",
        "layers/surface_story/tones",
    ),
    (
        "text outside safe/spoiler",
        "llm",
        "layers/surface_story/note",
        "x",
        "additionalProperties",
        "layers/surface_story",
    ),
    (
        "spoiler key in safe_text",
        "llm",
        "layers/surface_story/safe_text/resolution",
        "x",
        "additionalProperties",
        "layers/surface_story/safe_text",
    ),
    (
        "logline too long",
        "llm",
        "layers/surface_story/safe_text/logline",
        "x" * 241,
        "maxLength",
        "layers/surface_story/safe_text/logline",
    ),
    ("movie with series_status", "llm", "title/series_status", "ended", "not", "title"),
    ("tv without series_status", "llm", "title/media_type", "tv_series", "required", "title"),
    ("wrong schema_version", "llm", "schema_version", "9.9.9", "const", "schema_version"),
    ("annotated with abstain_reason", "llm", "abstain_reason", "summary_too_thin", "not", ""),
    ("abstained without reason", "abstained", "abstain_reason", DELETE, "required", ""),
    # jsonschema reports `false`-schema failures with keyword None at the parent's path
    ("abstained with beat tags", "abstained", "beat_tags", FROM_BASE, None, ""),
    ("abstained with layers", "abstained", "layers", FROM_BASE, None, ""),
    ("gold without sources", "gold", "provenance/sources", [], "minItems", "provenance/sources"),
    # TMDB text may not be an annotation input until TMDB authorizes LLM use in writing
    (
        "llm with tmdb_overview source",
        "llm",
        "provenance/sources/0/kind",
        "tmdb_overview",
        "const",
        "provenance/sources/0/kind",
    ),
    (
        "gold with tmdb_overview source",
        "gold",
        "provenance/sources/0/kind",
        "tmdb_overview",
        "const",
        "provenance/sources/0/kind",
    ),
    (
        "gold without guide_version",
        "gold",
        "provenance/annotator/guide_version",
        DELETE,
        "required",
        "provenance/annotator",
    ),
    (
        "gold without stages",
        "gold",
        "layers/mythic_blueprint/stages",
        DELETE,
        "required",
        "layers/mythic_blueprint",
    ),
    ("gold without tag judgments", "gold", "beat_tags/tags", DELETE, "required", "beat_tags"),
]


def mutate(base: str, path: str, value: Any) -> dict[str, Any]:
    rec = BASES[base]()
    *parents, last = path.split("/")
    node: Any = rec
    for key in parents:
        node = node[int(key)] if isinstance(node, list) else node[key]
    if value is FROM_BASE:  # a complete, valid block from the full example
        value = example("groundhog_day")[last]
    if value is DELETE:
        del node[last]
    else:
        node[last] = value
    return rec


def test_base_records_are_valid() -> None:
    for make in BASES.values():
        assert validate_record(make()) == []


@pytest.mark.parametrize(
    ("case", "base", "path", "value", "keyword", "where"),
    REJECT_CASES,
    ids=[c[0] for c in REJECT_CASES],
)
def test_schema_rejects(
    case: str, base: str, path: str, value: Any, keyword: str | None, where: str
) -> None:
    errors = list(validator().iter_errors(mutate(base, path, value)))
    assert errors, f"schema accepted: {case}"
    found = {(e.validator, "/".join(map(str, e.absolute_path))) for e in errors}
    assert (keyword, where) in found, f"expected {(keyword, where)}, got {found}"


def test_semantic_checks_catch_errors() -> None:
    def errs(fn: Any) -> list[str]:
        rec = llm_record()
        fn(rec)
        assert list(validator().iter_errors(rec)) == [], "must be schema-valid"
        return semantic_errors(rec)

    plots = ("layers", "archetypal_plot", "plots")
    skel = ("layers", "structural_skeleton")

    def primary_absent(r: dict) -> None:
        r[plots[0]][plots[1]][plots[2]]["rebirth"]["present"] = False

    def three_secondary(r: dict) -> None:
        for p in ("the_quest", "tragedy"):
            r[plots[0]][plots[1]][plots[2]][p]["present"] = True

    def evidence_absent(r: dict) -> None:
        r["beat_tags"]["spoiler_text"]["evidence"].append({"tag": "found_family", "note": "x"})

    def evidence_duplicate(r: dict) -> None:
        ev = r["beat_tags"]["spoiler_text"]["evidence"]
        ev.append(dict(ev[0]))

    def wrong_arc(r: dict) -> None:
        r[skel[0]][skel[1]]["emotional_arc"]["label"] = "icarus"

    def fallback_conf(r: dict) -> None:
        r[skel[0]][skel[1]]["arc_points"] = [0.1] * 11
        r[skel[0]][skel[1]]["emotional_arc"].update(
            label="riches_to_rags", net_change_fallback=True, confidence=0.8
        )

    def word_count(r: dict) -> None:
        r["provenance"]["input_word_count"] = 500

    for fn, needle in [
        (primary_absent, "primary plot"),
        (three_secondary, "secondary plots"),
        (evidence_absent, "judged absent"),
        (evidence_duplicate, "more than once"),
        (wrong_arc, "does not match derivation"),
        (fallback_conf, "below 0.5"),
        (word_count, "input_word_count"),
    ]:
        found = errs(fn)
        assert any(needle in e for e in found), (fn.__name__, found)


# ---------- vocabulary: schema <-> doc, mechanically ----------


def test_all_enums_are_named_defs() -> None:
    """Every enum lives directly in $defs, so the doc can name it."""

    def find(node: Any, path: str) -> Iterator[str]:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "enum" and not re.fullmatch(r"/\$defs/[a-z_]+", path):
                    yield path
                yield from find(v, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                yield from find(v, f"{path}/{i}")

    assert list(find(load_schema(), "")) == []


def test_doc_vocabularies_match_schema_enums() -> None:
    enums = schema_enums()
    doc = parse_vocab_tables(doc_text())
    assert set(doc) == set(enums), (
        f"only in doc: {sorted(set(doc) - set(enums))}; "
        f"only in schema: {sorted(set(enums) - set(doc))}"
    )
    for name, values in enums.items():
        assert len(values) == len(set(values)), f"duplicate enum values in {name}"
        doc_values = [row[0].strip("`") for row in doc[name]]
        assert doc_values == values, f"{name}: doc {doc_values} != schema {values}"


def test_vocab_parser_rejects_non_vocabulary_rows() -> None:
    good = (
        "<!-- vocab:x -->\n"
        "| Value | Name | Definition |\n"
        "|---|---|---|\n"
        "| `a` | A | defined here |\n"
    )
    assert parse_vocab_tables(good) == {"x": [["`a`", "A", "defined here"]]}
    for bad_row in ("| a | A | no backticks |", "| `a` extra | A | junk |", "| | A | empty |"):
        with pytest.raises(ValueError, match="non-vocabulary row"):
            parse_vocab_tables(good + bad_row + "\n")
    with pytest.raises(ValueError, match="separator"):
        parse_vocab_tables("<!-- vocab:x -->\n| Value |\n| `a` |\n")


def test_every_doc_term_has_a_definition() -> None:
    for name, rows in parse_vocab_tables(doc_text()).items():
        for row in rows:
            assert len(row) in (3, 4), f"{name}: malformed row {row}"
            assert len(row[-1]) >= 20, f"{name}: {row[0]} has no real definition"


def test_backticked_identifiers_in_doc_exist() -> None:
    """Snake_case identifiers in backticks must be a schema term, field or def (catches renames)."""
    record = load_schema()
    known: set[str] = set(record["$defs"])
    for values in schema_enums().values():
        known.update(v for v in values if isinstance(v, str))

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            known.update(node.get("properties", {}))
            for v in node.values():
                collect(v)
        elif isinstance(node, list):
            for v in node:
                collect(v)

    collect(record)
    collect(model_output_schema())
    json_schema_words = {"minimum", "maximum", "if", "then", "const", "false", "true", "null"}
    top = set(record["properties"]) | set(record["$defs"])

    text = re.sub(r"```.*?```", "", doc_text(), flags=re.S)
    unknown = []
    for token in re.findall(r"`([^`\n]+)`", text):
        if re.fullmatch(r"[a-z][a-z0-9_]*", token):
            if token not in known | json_schema_words:
                unknown.append(token)
        elif re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", token):
            parts = token.split(".")
            if parts[0] in top:
                unknown.extend(p for p in parts if p not in known)
    assert unknown == [], f"backticked identifiers not in schema: {sorted(set(unknown))}"


def test_spoiler_levels_cover_exactly_the_viewer_visible_vocabularies() -> None:
    schema = load_schema()
    shown: set[str] = set()
    for root_name, def_name in viewer_roots():
        for _, raw in walk(schema, {"$ref": f"#/$defs/{def_name}"}, (root_name,)):
            name = ref_name(raw)
            if name and "enum" in schema["$defs"][name]:
                shown.add(name)
        for _, raw in walk(schema, {"$ref": f"#/$defs/{def_name}"}, (root_name,)):
            node = resolve(schema, raw)
            if isinstance(node, dict) and "x-laminary-keys" in node:
                shown.add(node["x-laminary-keys"])
    shown.discard("arc_method")  # internal provenance of the arc label, never displayed
    levels = schema["x-laminary-spoiler-levels"]["vocabularies"]
    assert set(levels) == shown
    allowed = set(schema["$defs"]["spoiler_level"]["enum"])
    for name, mapping in levels.items():
        assert list(mapping) == schema["$defs"][name]["enum"], name
        assert set(mapping.values()) <= allowed, name


def test_doc_spoiler_column_matches_schema() -> None:
    levels = load_schema()["x-laminary-spoiler-levels"]["vocabularies"]
    for name, rows in parse_vocab_tables(doc_text()).items():
        if name in levels:
            for row in rows:
                assert len(row) == 4, f"{name}: row needs a spoiler column: {row}"
                assert row[2] == levels[name][row[0].strip("`")], f"{name}: {row[0]}"
        else:
            assert all(len(row) == 3 for row in rows), f"{name}: unexpected spoiler column"


def test_fixed_key_objects_list_every_term() -> None:
    schema = load_schema()
    found = 0
    for node in schema["$defs"].values():
        stack = [node]
        while stack:
            n = stack.pop()
            if isinstance(n, dict):
                if "x-laminary-keys" in n:
                    found += 1
                    terms = schema["$defs"][n["x-laminary-keys"]]["enum"]
                    assert list(n["properties"]) == terms
                    assert n["required"] == terms
                    assert n["additionalProperties"] is False
                stack.extend(n.values())
            elif isinstance(n, list):
                stack.extend(n)
    assert found == 6  # plots, stages, tags; full and gold variants


def test_single_choice_fields_have_max_field_level() -> None:
    """Hidden single-choice fields must hide whatever their value (QA S1)."""
    schema = load_schema()
    vocab_levels = schema["x-laminary-spoiler-levels"]["vocabularies"]
    fields = schema["x-laminary-spoiler-levels"]["fields"]
    single: dict[str, str] = {}
    for path, raw in walk(schema, {"$ref": "#/$defs/layers"}, ("layers",)):
        name = ref_name(raw)
        if name in vocab_levels and path[-1] != "[]":
            key = ".".join(path[:-1]) if path[-1] == "label" else ".".join(path)
            single[key] = name
    assert set(single) <= set(fields)
    for key, name in single.items():
        want = max(vocab_levels[name].values(), key=LEVEL_ORDER.__getitem__)
        assert fields[key] == want, key
    paths = {".".join(p) for p, _ in walk(schema, {"$ref": "#/$defs/layers"}, ("layers",))}
    assert set(fields) <= paths


# ---------- spoiler separation ----------


def test_all_free_text_is_inside_safe_or_spoiler_text() -> None:
    schema = load_schema()
    text_paths: list[tuple[str, ...]] = []
    for root_name, def_name in viewer_roots():
        for path, raw in walk(schema, {"$ref": f"#/$defs/{def_name}"}, (root_name,)):
            node = resolve(schema, raw)
            if isinstance(node, dict) and is_string_type(node) and "enum" not in node:
                text_paths.append(path)
    assert text_paths, "expected some free-text fields"
    bad = [p for p in text_paths if "safe_text" not in p and "spoiler_text" not in p]
    assert not bad, f"free text outside safe_text/spoiler_text: {bad}"


def test_free_text_walk_sees_nullable_string_types() -> None:
    fake = {"$defs": {}, "type": "object", "properties": {"x": {"type": ["string", "null"]}}}
    nodes = [resolve(fake, raw) for _, raw in walk(fake, fake, ("root",))]
    assert any(is_string_type(n) for n in nodes)
