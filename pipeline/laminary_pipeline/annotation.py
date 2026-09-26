"""Load and validate annotation records against the packaged JSON schema.

Validation has two parts:
- JSON Schema (``laminary_pipeline/schema/annotation.schema.json``), with format checking on;
- semantic checks that JSON Schema can't express (docs/NARRATIVE_SCHEMA.md section 11).
"""

from __future__ import annotations

import json
from datetime import datetime
from functools import cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from laminary_pipeline.arc import FALLBACK_CONFIDENCE_CAP, derive_arc

SCHEMA_RESOURCE = ("laminary_pipeline", "schema/annotation.schema.json")
MAX_SECONDARY_PLOTS = 2


@cache
def _schema_text() -> str:
    package, path = SCHEMA_RESOURCE
    return resources.files(package).joinpath(path).read_text(encoding="utf-8")


def load_schema() -> dict[str, Any]:
    """A fresh copy of the annotation schema (callers may mutate it)."""
    return json.loads(_schema_text())


def schema_version() -> str:
    return load_schema()["properties"]["schema_version"]["const"]


def _is_rfc3339_datetime(value: object) -> bool:
    """Strict-enough RFC 3339 date-time: 'T' separator and an explicit offset or 'Z'."""
    if not isinstance(value, str):
        return True  # format only constrains strings
    if len(value) < 20 or value[10] not in "Tt":
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("z", "Z"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def format_checker() -> FormatChecker:
    """jsonschema's format checker, with a stdlib date-time check.

    jsonschema only checks 'date-time' when the optional rfc3339-validator package is
    installed and silently passes otherwise, so we register our own.
    """
    checker = FormatChecker()
    checker.checks("date-time")(_is_rfc3339_datetime)
    return checker


def validator() -> Draft202012Validator:
    return Draft202012Validator(load_schema(), format_checker=format_checker())


def semantic_errors(record: dict[str, Any]) -> list[str]:
    """Checks from docs/NARRATIVE_SCHEMA.md section 11. Assumes the record is schema-valid."""
    errors: list[str] = []
    if record.get("record_kind") == "llm_annotation":
        prov = record["provenance"]
        total = sum(src["word_count"] for src in prov["sources"])
        if prov["input_word_count"] != total:
            errors.append(f"input_word_count {prov['input_word_count']} != sum of sources {total}")
    if record.get("outcome") != "annotated":
        return errors
    layers = record["layers"]

    plot = layers["archetypal_plot"]
    primary = plot["primary"]["label"]
    if not plot["plots"][primary]["present"]:
        errors.append(f"primary plot {primary} is not marked present in plots")
    others = [p for p, j in plot["plots"].items() if j["present"] and p != primary]
    if len(others) > MAX_SECONDARY_PLOTS:
        errors.append(f"more than {MAX_SECONDARY_PLOTS} secondary plots present: {others}")

    beats = record["beat_tags"]
    evidence_tags = [e["tag"] for e in beats.get("spoiler_text", {}).get("evidence", [])]
    if len(evidence_tags) != len(set(evidence_tags)):
        errors.append("evidence lists a tag more than once")
    absent = [t for t in evidence_tags if not beats["tags"][t]["present"]]
    if absent:
        errors.append(f"evidence for tags judged absent: {absent}")

    skel = layers["structural_skeleton"]
    arc = skel["emotional_arc"]
    if arc["method"] == "derived":
        if "arc_points" not in skel:
            errors.append("derived emotional_arc without arc_points")
        else:
            d = derive_arc(skel["arc_points"])
            got = (arc["label"], arc["threshold_used"], arc["net_change_fallback"])
            want = (d.label, d.threshold_used, d.net_change_fallback)
            if got != want:
                errors.append(f"emotional_arc {got} does not match derivation {want}")
            conf = arc.get("confidence")
            if d.net_change_fallback and conf is not None and conf > FALLBACK_CONFIDENCE_CAP:
                errors.append("net-change fallback arc must have confidence below 0.5")
    return errors


def validate_record(record: dict[str, Any]) -> list[str]:
    """All problems with a record: schema errors first; semantic checks only if schema-valid."""
    schema_errors = [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in sorted(validator().iter_errors(record), key=lambda e: list(e.absolute_path))
    ]
    return schema_errors or semantic_errors(record)
