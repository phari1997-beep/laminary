"""Derive the emotional-arc label from arc points (docs/NARRATIVE_SCHEMA.md section 8).

The model outputs only the 11 arc points. The label is computed here with a fixed rule so the
annotation prompt, the gold-set guide and the pipeline all agree:

1. A *major move* is a rise or fall of at least ``threshold`` (start 0.3) from the most recent
   peak or trough. Smaller wobbles are ignored. Deltas are rounded to 6 decimals before the
   comparison, so a move of exactly 0.3 counts despite float error (0.7 - 0.4 = 0.2999...).
2. One to three major moves map directly to one of the six arcs.
3. Four or more moves: raise the threshold in 0.1 steps until at most three remain.
4. No major moves (a flat or gentle story, at any threshold): fall back to the direction of the
   net change, end minus start (rise -> rags_to_riches, fall -> riches_to_rags). A net change of
   exactly zero counts as a fall. Fallback labels have confidence capped below 0.5.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

ARC_POINT_COUNT = 11
MAJOR_MOVE = 0.3
THRESHOLD_STEP = 0.1
FALLBACK_CONFIDENCE_CAP = 0.49
_DECIMALS = 6

LEGS_TO_ARC: dict[tuple[str, ...], str] = {
    ("up",): "rags_to_riches",
    ("down",): "riches_to_rags",
    ("down", "up"): "man_in_a_hole",
    ("up", "down"): "icarus",
    ("up", "down", "up"): "cinderella",
    ("down", "up", "down"): "oedipus",
}


@dataclass(frozen=True)
class ArcDerivation:
    label: str
    threshold_used: float
    legs: tuple[str, ...]
    net_change_fallback: bool


def _reaches(delta: float, threshold: float) -> bool:
    return round(delta, _DECIMALS) >= round(threshold, _DECIMALS)


def major_moves(points: Sequence[float], threshold: float = MAJOR_MOVE) -> tuple[str, ...]:
    """Directions ("up"/"down") of the moves of at least ``threshold`` from the last extreme."""
    legs: list[str] = []
    direction: str | None = None
    lo = hi = extreme = points[0]
    for p in points[1:]:
        if direction is None:
            lo, hi = min(lo, p), max(hi, p)
            if _reaches(p - lo, threshold):
                direction, extreme = "up", p
                legs.append("up")
            elif _reaches(hi - p, threshold):
                direction, extreme = "down", p
                legs.append("down")
        elif direction == "up":
            if p > extreme:
                extreme = p
            elif _reaches(extreme - p, threshold):
                direction, extreme = "down", p
                legs.append("down")
        else:
            if p < extreme:
                extreme = p
            elif _reaches(p - extreme, threshold):
                direction, extreme = "up", p
                legs.append("up")
    return tuple(legs)


def derive_arc(points: Sequence[float]) -> ArcDerivation:
    """Apply the section 8 rule to exactly 11 points in [-1, 1]."""
    if len(points) != ARC_POINT_COUNT:
        raise ValueError(f"expected {ARC_POINT_COUNT} arc points, got {len(points)}")
    if any(not -1 <= p <= 1 for p in points):
        raise ValueError("arc points must be in [-1, 1]")
    threshold = MAJOR_MOVE
    while True:
        legs = major_moves(points, threshold)
        if 1 <= len(legs) <= 3:
            return ArcDerivation(LEGS_TO_ARC[legs], round(threshold, 1), legs, False)
        if not legs:
            net = round(points[-1] - points[0], _DECIMALS)
            label = "rags_to_riches" if net > 0 else "riches_to_rags"
            return ArcDerivation(label, round(threshold, 1), (), True)
        threshold += THRESHOLD_STEP  # 4+ moves: look at a coarser scale


def derived_confidence(model_confidence: float, derivation: ArcDerivation) -> float:
    """Confidence stored for a derived label: the model's, capped for net-change fallbacks."""
    if derivation.net_change_fallback:
        return min(model_confidence, FALLBACK_CONFIDENCE_CAP)
    return model_confidence
