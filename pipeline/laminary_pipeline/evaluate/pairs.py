""" "Should match / should NOT match" pair check (interview risk 1). STUB: interface and report
format only; it runs once narrative embeddings exist.

docs/research/PHASE0_INTERVIEWS.md risk 1: structure alone produces absurd matches (Finding
Nemo and Taken are both "a dad finds his lost kid"). The gold set therefore carries title pairs
with an expectation. Once fingerprints exist, a similarity function scores each pair, and the
check reports pairs on the wrong side of the match threshold.

Pairs file (CSV, header row required; the gold tooling owns the file, expected at
``pipeline/data/config/similarity_pairs.csv``)::

    title_a,title_b,expectation,note
    movie:12,movie:8681,should_not_match,"same shape, different genre/tone/audience"

``title_a``/``title_b`` are title keys (``<media_type>:<tmdb_id>``) or Wikidata QIDs;
``expectation`` is ``should_match`` or ``should_not_match``.
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

EXPECTATIONS = ("should_match", "should_not_match")
NOT_RUN = "not run: needs narrative embeddings (Phase 1 embeddings task)"

# (title_a, title_b) -> similarity in [0, 1], or None when either title has no fingerprint.
Similarity = Callable[[str, str], float | None]


@dataclass(frozen=True)
class PairExpectation:
    title_a: str
    title_b: str
    expectation: str
    note: str = ""


@dataclass(frozen=True)
class PairResult:
    pair: PairExpectation
    similarity: float | None
    matched: bool | None  # similarity >= threshold
    ok: bool | None  # matched agrees with the expectation; None when not scored


@dataclass
class PairReport:
    status: str
    threshold: float | None
    results: list[PairResult] = field(default_factory=list)

    @property
    def violations(self) -> list[PairResult]:
        return [r for r in self.results if r.ok is False]

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "threshold": self.threshold,
            "pairs": len(self.results),
            "scored": sum(r.ok is not None for r in self.results),
            "violations": len(self.violations),
            "results": [
                {
                    "title_a": r.pair.title_a,
                    "title_b": r.pair.title_b,
                    "expectation": r.pair.expectation,
                    "similarity": r.similarity,
                    "matched": r.matched,
                    "ok": r.ok,
                    "note": r.pair.note,
                }
                for r in self.results
            ],
        }


def load_pairs(path: Path) -> list[PairExpectation]:
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    pairs = []
    for n, row in enumerate(rows, start=2):
        exp = (row.get("expectation") or "").strip()
        if exp not in EXPECTATIONS:
            raise ValueError(f"{path}:{n}: expectation {exp!r} not in {EXPECTATIONS}")
        a, b = (row.get("title_a") or "").strip(), (row.get("title_b") or "").strip()
        if not a or not b:
            raise ValueError(f"{path}:{n}: title_a and title_b are required")
        pairs.append(PairExpectation(a, b, exp, (row.get("note") or "").strip()))
    return pairs


def check_pairs(
    pairs: list[PairExpectation],
    similarity: Similarity | None = None,
    threshold: float | None = None,
) -> PairReport:
    """Score each pair. Without a similarity function (today), every pair is listed unscored."""
    if similarity is None or threshold is None:
        return PairReport(NOT_RUN, None, [PairResult(p, None, None, None) for p in pairs])
    results = []
    for p in pairs:
        s = similarity(p.title_a, p.title_b)
        if s is None:
            results.append(PairResult(p, None, None, None))
            continue
        matched = s >= threshold
        results.append(PairResult(p, s, matched, matched == (p.expectation == "should_match")))
    return PairReport("scored", threshold, results)


def render_markdown(report: PairReport) -> str:
    lines = [
        "## Should / should NOT match pairs",
        "",
        f"Status: {report.status}. Pairs: {len(report.results)}; "
        f"violations: {len(report.violations)}"
        + (f" (threshold {report.threshold})." if report.threshold is not None else "."),
        "",
    ]
    if report.results:
        lines += [
            "| Title A | Title B | Expectation | Similarity | Result | Note |",
            "|---|---|---|---:|---|---|",
        ]
        for r in report.results:
            sim = "" if r.similarity is None else f"{r.similarity:.3f}"
            result = {None: "not scored", True: "ok", False: "VIOLATION"}[r.ok]
            lines.append(
                f"| {r.pair.title_a} | {r.pair.title_b} | {r.pair.expectation} | {sim} | "
                f"{result} | {r.pair.note} |"
            )
    return "\n".join(lines)
