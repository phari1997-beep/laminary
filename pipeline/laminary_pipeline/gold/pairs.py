""""Should match" / "should NOT match" title pairs for evaluating similarity (interview risk 1).

``data/config/similarity_pairs.csv`` (hand-written, committed). Each pair shares a story shape;
``no_match`` pairs must not be recommended for each other (genre, tone or audience differ, e.g.
Finding Nemo vs Taken), ``match`` pairs should be. Hari reviews the list; ``status`` is
``seed`` / ``proposed`` / ``approved`` / ``rejected``.

Optional trailing columns ``qid_a`` / ``qid_b`` pin a side to one Wikidata QID when the title,
year and type fit more than one item (e.g. N13, Triangle (2009): the British film Q1783930, by
Hari's decision, DECISIONS 2026-10-03). A pin only chooses among items that already match the
title, type and year; it never overrides those checks. Blank means "resolve by title". One title
pinned to two different QIDs is an error.

``resolve_pairs`` maps titles to Wikidata QIDs through the candidate list (and, when given, the
pairs set from ``ingest pairs``, whose rows carry the pairs file's own title as ``pair_title``),
so the similarity evaluation can look up both titles' fingerprints later.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from laminary_pipeline.ingest.candidates import normalize_title

PAIR_COLUMNS = [
    "pair_id", "expect", "title_a", "year_a", "type_a", "title_b", "year_b", "type_b",
    "shared_shape", "why", "status",
]
EXPECT = frozenset({"match", "no_match"})
TYPES = frozenset({"movie", "tv_series"})
STATUSES = frozenset({"seed", "proposed", "approved", "rejected"})
PIN_COLUMNS = ["qid_a", "qid_b"]
QID_RE = re.compile(r"Q[1-9][0-9]*")


@dataclass(frozen=True)
class Pair:
    pair_id: str
    expect: str
    a: tuple[str, int, str]
    b: tuple[str, int, str]
    shared_shape: str
    why: str
    status: str
    pin_a: str | None = None  # qid_a: this side's pinned Wikidata QID, if any
    pin_b: str | None = None


def pins(pairs: Sequence[Pair]) -> dict[tuple[str, int, str], str]:
    """Each pinned pair title (title, year, type) and its QID."""
    out: dict[tuple[str, int, str], str] = {}
    for p in pairs:
        for side, pin in ((p.a, p.pin_a), (p.b, p.pin_b)):
            if pin:
                out[side] = pin
    return out


def load_pairs(path: Path) -> tuple[list[Pair], list[str]]:
    pairs: list[Pair] = []
    errors: list[str] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames not in (PAIR_COLUMNS, PAIR_COLUMNS + PIN_COLUMNS):
            errors.append(f"columns must be {PAIR_COLUMNS} (optionally followed by "
                          f"{PIN_COLUMNS}), got {reader.fieldnames}")
            return pairs, errors
        ids: set[str] = set()
        pinned: dict[tuple[str, int, str], str] = {}
        for i, row in enumerate(reader, start=2):
            problems = []
            if row["pair_id"] in ids:
                problems.append("duplicate pair_id")
            ids.add(row["pair_id"])
            if row["expect"] not in EXPECT:
                problems.append(f"expect must be one of {sorted(EXPECT)}")
            if row["status"] not in STATUSES:
                problems.append(f"status must be one of {sorted(STATUSES)}")
            for side in ("a", "b"):
                if row[f"type_{side}"] not in TYPES:
                    problems.append(f"type_{side} must be movie or tv_series")
                if not row[f"year_{side}"].isdigit():
                    problems.append(f"year_{side} must be a year")
                if not row[f"title_{side}"].strip():
                    problems.append(f"title_{side} is blank")
            if not row["shared_shape"].strip() or not row["why"].strip():
                problems.append("shared_shape and why are required")
            pin = {side: (row.get(f"qid_{side}") or "").strip() or None for side in "ab"}
            for side, qid in pin.items():
                if qid and not QID_RE.fullmatch(qid):
                    problems.append(f"qid_{side} must be a Wikidata QID (Q123) or blank")
            if problems:
                errors.append(f"row {i} ({row['pair_id']}): " + "; ".join(problems))
                continue
            a = (row["title_a"], int(row["year_a"]), row["type_a"])
            b = (row["title_b"], int(row["year_b"]), row["type_b"])
            for side, key in (("a", a), ("b", b)):
                qid = pin[side]
                if qid and pinned.setdefault(key, qid) != qid:
                    problems.append(f"qid_{side} {qid} conflicts with {pinned[key]} pinned "
                                    f"for {key[0]} ({key[1]}) in an earlier row")
            if problems:
                errors.append(f"row {i} ({row['pair_id']}): " + "; ".join(problems))
                continue
            pairs.append(Pair(row["pair_id"], row["expect"], a, b, row["shared_shape"],
                              row["why"], row["status"], pin["a"], pin["b"]))
    return pairs, errors


def _find(side: tuple[str, int, str], candidates: Sequence[dict[str, Any]],
          pin: str | None = None) -> str | None:
    title, year, media_type = side
    want = normalize_title(title)
    for c in candidates:
        if pin and c["qid"] != pin:
            continue
        names = {normalize_title(c.get(k) or "")
                 for k in ("title", "gold_seed_title", "pair_title")}
        close = bool(c.get("year")) and abs(c["year"] - year) <= 1
        if c["media_type"] == media_type and close and want in names:
            return c["qid"]
    return None


def resolve_pairs(
    pairs: Sequence[Pair], candidates: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        {"pair_id": p.pair_id, "expect": p.expect, "qid_a": _find(p.a, candidates, p.pin_a),
         "qid_b": _find(p.b, candidates, p.pin_b), "title_a": p.a[0], "title_b": p.b[0]}
        for p in pairs
    ]
