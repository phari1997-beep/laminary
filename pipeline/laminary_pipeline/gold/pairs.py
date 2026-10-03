""""Should match" / "should NOT match" title pairs for evaluating similarity (interview risk 1).

``data/config/similarity_pairs.csv`` (hand-written, committed). Each pair shares a story shape;
``no_match`` pairs must not be recommended for each other (genre, tone or audience differ, e.g.
Finding Nemo vs Taken), ``match`` pairs should be. Hari reviews the list; ``status`` is
``seed`` / ``proposed`` / ``approved`` / ``rejected``.

``resolve_pairs`` maps titles to Wikidata QIDs through the candidate list (and, when given, the
pairs set from ``ingest pairs``, whose rows carry the pairs file's own title as ``pair_title``),
so the similarity evaluation can look up both titles' fingerprints later.
"""

from __future__ import annotations

import csv
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


@dataclass(frozen=True)
class Pair:
    pair_id: str
    expect: str
    a: tuple[str, int, str]
    b: tuple[str, int, str]
    shared_shape: str
    why: str
    status: str


def load_pairs(path: Path) -> tuple[list[Pair], list[str]]:
    pairs: list[Pair] = []
    errors: list[str] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != PAIR_COLUMNS:
            errors.append(f"columns must be {PAIR_COLUMNS}, got {reader.fieldnames}")
            return pairs, errors
        ids: set[str] = set()
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
            if problems:
                errors.append(f"row {i} ({row['pair_id']}): " + "; ".join(problems))
                continue
            pairs.append(Pair(
                row["pair_id"], row["expect"],
                (row["title_a"], int(row["year_a"]), row["type_a"]),
                (row["title_b"], int(row["year_b"]), row["type_b"]),
                row["shared_shape"], row["why"], row["status"],
            ))
    return pairs, errors


def _find(side: tuple[str, int, str], candidates: Sequence[dict[str, Any]]) -> str | None:
    title, year, media_type = side
    want = normalize_title(title)
    for c in candidates:
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
        {"pair_id": p.pair_id, "expect": p.expect, "qid_a": _find(p.a, candidates),
         "qid_b": _find(p.b, candidates), "title_a": p.a[0], "title_b": p.b[0]}
        for p in pairs
    ]
