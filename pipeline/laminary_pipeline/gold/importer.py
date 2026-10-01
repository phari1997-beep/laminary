"""Import a filled gold sheet (CSV exported from Google Sheets) as ``gold_label`` records.

Every problem is reported per row, in plain words, with the column name, so a labeler can fix
the sheet. A row becomes a record only if it has no errors and the record passes
``annotation.validate_record`` (schema + semantic checks).

Rows that are completely unlabeled (no labeler id and no labels) are skipped, not errors, so a
partly filled sheet can be imported as work progresses.

Double labeling: ``label_slot`` is 1 (or blank) for the reference labeler and 2 for the second
labeler of a double-labeled title. Records are written with every slot-1 record before any
slot-2 record, because the evaluation takes the first record for a title as the reference
(``evaluate.report.index_gold``). Import the whole sheet into one file.
"""

from __future__ import annotations

import csv
import io
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from laminary_pipeline.annotation import schema_version, validate_record
from laminary_pipeline.arc import derive_arc
from laminary_pipeline.gold.columns import (
    ARC_NAMES,
    ARC_POINT_COLS,
    BLUEPRINT_NAMES,
    BOOKER_PLOTS,
    CONFIDENCE_VALUES,
    FLAT_ARCS,
    GUIDE_VERSION,
    HELP_MARKER,
    LABEL_SLOTS,
    LABELER_COLS,
    NO,
    PLOT_COLS,
    PLOT_NAMES,
    SKIP_NAMES,
    STAGE_COLS,
    STAGES,
    TAG_COLS,
    TAGS,
    YES,
)

REQUIRED_PREFILLED = [
    "qid", "title", "year", "type", "tmdb_id", "source_ref", "source_revision",
    "source_retrieved_at", "source_license", "source_word_count", "source_sha256",
]
QID_RE = re.compile(r"^Q[1-9][0-9]*$")
SOURCE_COLS = [
    "source_ref", "source_revision", "source_retrieved_at", "source_license",
    "source_word_count", "source_sha256",
]
MAX_SECONDARY = 2


@dataclass
class RowProblem:
    row: int  # spreadsheet row number (header = 1)
    title: str
    messages: list[str]

    def __str__(self) -> str:
        return f"Row {self.row} ({self.title or 'untitled'}):\n  - " + "\n  - ".join(self.messages)


@dataclass
class ImportResult:
    records: list[dict[str, Any]] = field(default_factory=list)
    errors: list[RowProblem] = field(default_factory=list)
    warnings: list[RowProblem] = field(default_factory=list)
    skipped_unlabeled: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


def _lookup(value: str, names: dict[str, str]) -> str | None:
    """Schema key for a display name or a key, ignoring case and extra spaces."""
    v = re.sub(r"\s+", " ", value).strip().casefold()
    for key, name in names.items():
        if v in (key.casefold(), name.casefold()):
            return key
    return None


def _norm_header(h: str) -> str:
    return re.sub(r"\s+", "_", h.strip().lower())


def _yes_no(value: str) -> bool | None:
    v = value.strip().lower()
    if v in YES:
        return True
    if v in NO:
        return False
    return None


def _labeled(row: dict[str, str]) -> bool:
    return any(row.get(c, "").strip() for c in LABELER_COLS if c != "notes")


def _judgments(row: dict[str, str], cols: list[str], keys: list[str], what: str,
               errors: list[str]) -> dict[str, dict[str, bool]]:
    out: dict[str, dict[str, bool]] = {}
    blank: list[str] = []
    bad: list[str] = []
    for col, key in zip(cols, keys, strict=True):
        raw = row.get(col, "")
        val = _yes_no(raw)
        if val is not None:
            out[key] = {"present": val}
        elif not raw.strip():
            blank.append(col)
        else:
            bad.append(f"{col}={raw.strip()!r}")
    if blank:
        errors.append(f"{what}: enter Y or N in every column; blank: {', '.join(blank)}")
    if bad:
        errors.append(f"{what}: only Y or N allowed; got {', '.join(bad)}")
    return out


def _arc(
    row: dict[str, str], errors: list[str], warnings: list[str]
) -> tuple[dict[str, Any] | None, list[float] | None]:
    shape_raw = row.get("arc_shape", "").strip()
    point_raw = [row.get(c, "").strip() for c in ARC_POINT_COLS]
    filled = [p for p in point_raw if p]
    points: list[float] | None = None
    if filled and len(filled) != len(point_raw):
        missing = [c for c, p in zip(ARC_POINT_COLS, point_raw, strict=True) if not p]
        errors.append(f"arc points: fill all 11 or none; missing {', '.join(missing)}")
    elif filled:
        try:
            points = [float(p.replace(",", ".")) for p in point_raw]
        except ValueError:
            errors.append("arc points must be numbers between -1 and 1")
            points = None
        if points is not None:
            if any(math.isnan(p) or not -1 <= p <= 1 for p in points):
                errors.append("arc points must be between -1 and 1")
                points = None
    if shape_raw:
        flat = next((k for k in FLAT_ARCS if k.casefold() == shape_raw.casefold()), None)
        if flat:
            label, fallback = FLAT_ARCS[flat], True
        else:
            label = _lookup(shape_raw, ARC_NAMES)
            fallback = False
            if label is None:
                allowed = " / ".join([*ARC_NAMES.values(), *FLAT_ARCS])
                errors.append(f"arc_shape {shape_raw!r} is not one of: {allowed}")
                return None, points
        arc = {"label": label, "method": "labeler_assigned", "net_change_fallback": fallback}
        if points is not None:
            derived = derive_arc(points)
            if derived.label != label or derived.net_change_fallback != fallback:
                warnings.append(
                    f"arc_shape says {label}{' (flat)' if fallback else ''} but the 11 points "
                    f"trace {derived.label}{' (flat)' if derived.net_change_fallback else ''}; "
                    "kept arc_shape"
                )
        return arc, points
    if points is not None:
        d = derive_arc(points)
        arc = {
            "label": d.label, "method": "derived", "threshold_used": d.threshold_used,
            "net_change_fallback": d.net_change_fallback, "reduced_shape": d.reduced_shape,
        }
        return arc, points
    if not filled:
        errors.append("emotional arc: choose an arc_shape, or fill all 11 arc points")
    return None, None


def _int(value: str) -> int | None:
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return None


def row_to_record(
    row: dict[str, str], annotated_at: str
) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    for col in REQUIRED_PREFILLED:
        if not row.get(col, "").strip():
            hint = " (no TMDB id in Wikidata: drop this title)" if col == "tmdb_id" else ""
            errors.append(f"prefilled column {col} is empty{hint}; don't edit prefilled columns")
    if errors:
        return None, errors, warnings
    qid = row["qid"].strip()
    if not QID_RE.match(qid):
        errors.append(f"qid {qid!r} is not a Wikidata id like Q12345")
    media_type = row["type"].strip()
    if media_type not in ("movie", "tv_series"):
        errors.append(f"type {media_type!r} must be movie or tv_series")
    year, tmdb_id = _int(row["year"]), _int(row["tmdb_id"])
    if year is None:
        errors.append(f"year {row['year']!r} is not a number")
    if tmdb_id is None:
        errors.append(f"tmdb_id {row['tmdb_id']!r} is not a number")
    # Series summarized from season articles carry one value per article, separated by '|'.
    split = {c: [v.strip() for v in row[c].split("|")] for c in SOURCE_COLS}
    n_sources = len(split["source_ref"])
    if any(len(v) != n_sources for v in split.values()):
        errors.append("the source_ columns must list the same number of values; "
                      "don't edit prefilled columns")
        n_sources = 0
    word_counts = [_int(w) for w in split["source_word_count"][:n_sources]]
    if any(w is None for w in word_counts):
        errors.append(f"source_word_count {row['source_word_count']!r} is not a number")
    series_status = row.get("series_status", "").strip()
    if media_type == "tv_series" and series_status not in ("ended", "ongoing", "unknown"):
        errors.append("series_status must be 'ended', 'ongoing' or 'unknown' for a TV series")

    labeler = row.get("labeler_id", "").strip()
    if not labeler:
        errors.append("labeler_id is blank: enter your labeler code")

    title: dict[str, Any] = {
        "media_type": media_type, "name": row["title"].strip(), "release_year": year,
        "tmdb_id": tmdb_id, "wikidata_id": qid,
    }
    if media_type == "tv_series":
        title["series_status"] = series_status
    provenance: dict[str, Any] = {
        "annotated_at": annotated_at,
        "annotator": {
            "labeler_id": labeler,
            "guide_version": row.get("guide_version", "").strip() or GUIDE_VERSION,
        },
        "sources": [
            {
                "kind": "wikipedia_plot",
                "ref": split["source_ref"][i],
                "revision": split["source_revision"][i],
                "retrieved_at": split["source_retrieved_at"][i],
                "license": split["source_license"][i],
                "word_count": word_counts[i],
                "content_sha256": split["source_sha256"][i],
            }
            for i in range(n_sources)
        ],
        "input_word_count": sum(w or 0 for w in word_counts),
    }
    notes = row.get("notes", "").strip()
    if notes:
        provenance["notes"] = notes[:500]
        if len(notes) > 500:
            warnings.append("notes longer than 500 characters were cut")
    record: dict[str, Any] = {
        "schema_version": schema_version(),
        "record_kind": "gold_label",
        "title": title,
        "provenance": provenance,
    }

    skip_raw = row.get("skip_reason", "").strip()
    if skip_raw:
        reason = _lookup(skip_raw, SKIP_NAMES)
        if reason is None:
            allowed = " / ".join(SKIP_NAMES.values())
            errors.append(f"skip_reason {skip_raw!r} is not one of: {allowed}")
        else:
            record.update(outcome="abstained", abstain_reason=reason)
            if any(row.get(c, "").strip() for c in LABELER_COLS
                   if c not in ("labeler_id", "skip_reason", "notes", "confidence")):
                warnings.append("skip_reason is set, so the labels on this row were ignored")
        return (None if errors else record), errors, warnings

    primary_raw = row.get("primary_plot", "").strip()
    primary = _lookup(primary_raw, PLOT_NAMES) if primary_raw else None
    if not primary_raw:
        errors.append("primary_plot is blank")
    elif primary is None:
        allowed = " / ".join(PLOT_NAMES.values())
        errors.append(f"primary_plot {primary_raw!r} is not one of: {allowed}")
    plots = _judgments(row, PLOT_COLS, BOOKER_PLOTS, "plots", errors)
    if primary and primary in plots and not plots[primary]["present"]:
        errors.append(f"the primary plot must be marked Y in plot_{primary}")
    if len(plots) == len(BOOKER_PLOTS):
        others = [k for k, v in plots.items() if v["present"] and k != primary]
        if len(others) > MAX_SECONDARY:
            errors.append(f"at most {MAX_SECONDARY} plots besides the primary can be Y; "
                          f"got {', '.join('plot_' + o for o in others)}")

    bp_raw = row.get("blueprint", "").strip()
    blueprint = _lookup(bp_raw, BLUEPRINT_NAMES) if bp_raw else None
    if not bp_raw:
        errors.append("blueprint is blank")
    elif blueprint is None:
        allowed = " / ".join(BLUEPRINT_NAMES.values())
        errors.append(f"blueprint {bp_raw!r} is not one of: {allowed}")
    stages = _judgments(row, STAGE_COLS, STAGES, "stages", errors)
    arc, points = _arc(row, errors, warnings)
    tags = _judgments(row, TAG_COLS, TAGS, "tags", errors)

    conf_raw = row.get("confidence", "").strip()
    confidence = None
    if conf_raw:
        match = next((k for k in CONFIDENCE_VALUES if k.casefold() == conf_raw.casefold()), None)
        if match is None:
            errors.append(f"confidence {conf_raw!r} is not one of: {' / '.join(CONFIDENCE_VALUES)}")
        else:
            confidence = CONFIDENCE_VALUES[match]
    if errors:
        return None, errors, warnings

    primary_obj: dict[str, Any] = {"label": primary}
    if confidence is not None:
        primary_obj["confidence"] = confidence
    skeleton: dict[str, Any] = {"emotional_arc": arc}
    if points is not None:
        skeleton["arc_points"] = points
    record.update(
        outcome="annotated",
        layers={
            "archetypal_plot": {"primary": primary_obj, "plots": plots},
            "mythic_blueprint": {"blueprint": {"label": blueprint}, "stages": stages},
            "structural_skeleton": skeleton,
        },
        beat_tags={"tags": tags},
    )
    return record, errors, warnings


def read_rows(text: str) -> list[tuple[int, dict[str, str]]]:
    """CSV rows keyed by normalized header, with their spreadsheet row numbers."""
    reader = csv.reader(io.StringIO(text))
    try:
        header = [_norm_header(h) for h in next(reader)]
    except StopIteration:
        return []
    rows = []
    for i, cells in enumerate(reader, start=2):
        if not cells or not any(c.strip() for c in cells):
            continue
        row = {h: (cells[j] if j < len(cells) else "") for j, h in enumerate(header)}
        if cells[0].strip().startswith(HELP_MARKER) or row.get("qid", "").strip().startswith(
            HELP_MARKER
        ):
            continue  # the help row (found by its qid cell even if columns were moved)
        rows.append((i, row))
    return rows


def import_csv_text(text: str, *, annotated_at: str | None = None) -> ImportResult:
    stamp = annotated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = ImportResult()
    seen: dict[tuple[str, str], int] = {}
    seen_slot: dict[tuple[str, str], int] = {}
    slot_of: dict[int, str] = {}
    for rownum, row in read_rows(text.lstrip("\ufeff")):
        title = row.get("title", "").strip()
        if not _labeled(row):
            result.skipped_unlabeled += 1
            continue
        record, errors, warnings = row_to_record(row, stamp)
        qid = row.get("qid", "").strip()
        key = (qid, row.get("labeler_id", "").strip())
        if key in seen:
            errors.append(f"labeler {key[1]!r} already labeled {key[0]} on row {seen[key]}")
        else:
            seen[key] = rownum
        slot = row.get("label_slot", "").strip() or "1"
        if slot not in LABEL_SLOTS:
            errors.append(f"label_slot {slot!r} must be 1 or 2; don't edit prefilled columns")
        elif (qid, slot) in seen_slot:
            errors.append(f"label_slot {slot} of {qid} is already labeled on row "
                          f"{seen_slot[(qid, slot)]}")
        else:
            seen_slot[(qid, slot)] = rownum
        if record is not None and not errors:
            problems = validate_record(record)
            if problems:
                errors.extend(f"schema check: {p}" for p in problems)
        if warnings:
            result.warnings.append(RowProblem(rownum, title, warnings))
        if errors:
            result.errors.append(RowProblem(rownum, title, errors))
        elif record is not None:
            slot_of[id(record)] = slot
            result.records.append(record)
    result.records.sort(key=lambda r: slot_of[id(r)])  # stable: slot 1 first, then sheet order
    return result


def import_csv(path: Path, *, annotated_at: str | None = None) -> ImportResult:
    return import_csv_text(path.read_text(encoding="utf-8-sig"), annotated_at=annotated_at)


def format_problems(problems: Iterable[RowProblem]) -> str:
    return "\n".join(str(p) for p in problems)
