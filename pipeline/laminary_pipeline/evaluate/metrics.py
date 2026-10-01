"""Small, dependency-free metric functions used by the evaluation report."""

from __future__ import annotations

from collections import Counter
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass

from laminary_pipeline.annotation import DISPLAY_CONFIDENCE_THRESHOLD

# Confidence bands (lower bound inclusive): docs/NARRATIVE_SCHEMA.md section 4, with the top
# band split exactly at the display threshold so calibration of shown labels is reported on its
# own, and 0.80-0.89 kept apart from 0.70-0.79.
BANDS: tuple[tuple[str, float], ...] = (
    ("0.95-1.00", DISPLAY_CONFIDENCE_THRESHOLD),  # shown in the app
    ("0.90-0.94", 0.90),
    ("0.80-0.89", 0.80),
    ("0.70-0.79", 0.70),
    ("0.50-0.69", 0.50),
    ("<0.50", float("-inf")),
)
DISPLAY_BAND = BANDS[0][0]


def ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def cohen_kappa(a: Sequence[Hashable], b: Sequence[Hashable]) -> float | None:
    """Cohen's kappa for two raters over the same items. None when undefined (no items, or
    chance agreement is 1, i.e. both raters used one identical category throughout)."""
    if len(a) != len(b):
        raise ValueError("raters must label the same items")
    n = len(a)
    if n == 0:
        return None
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    expected = sum(ca[k] * cb[k] for k in ca.keys() | cb.keys()) / (n * n)
    if expected == 1:
        return None
    return (observed - expected) / (1 - expected)


@dataclass
class Confusion:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    def add(self, gold: bool, predicted: bool) -> None:
        if gold and predicted:
            self.tp += 1
        elif predicted:
            self.fp += 1
        elif gold:
            self.fn += 1
        else:
            self.tn += 1

    @property
    def n(self) -> int:
        return self.tp + self.fp + self.fn + self.tn

    def summary(self) -> dict[str, float | int | None]:
        precision = ratio(self.tp, self.tp + self.fp)
        recall = ratio(self.tp, self.tp + self.fn)
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision is not None and recall is not None and precision + recall > 0
            else None
        )
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "accuracy": ratio(self.tp + self.tn, self.n),
        }


def band(confidence: float) -> str:
    for name, lower in BANDS:
        if confidence >= lower:
            return name
    raise AssertionError("unreachable")


def calibration(pairs: Iterable[tuple[float, bool]]) -> dict[str, dict[str, float | int | None]]:
    """Accuracy per confidence band from (confidence, correct) pairs."""
    counts: dict[str, list[int]] = {name: [0, 0] for name, _ in BANDS}
    for conf, correct in pairs:
        c = counts[band(conf)]
        c[0] += 1
        c[1] += int(correct)
    return {name: {"n": n, "accuracy": ratio(k, n)} for name, (n, k) in counts.items()}


def jaccard(a: Iterable[Hashable], b: Iterable[Hashable]) -> float:
    sa, sb = set(a), set(b)
    return 1.0 if not sa and not sb else len(sa & sb) / len(sa | sb)


def mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None
