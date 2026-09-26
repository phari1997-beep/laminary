"""Tests for the emotional-arc derivation rule (laminary_pipeline/arc.py, doc section 8)."""

from __future__ import annotations

import random

import pytest

from laminary_pipeline.arc import (
    FALLBACK_CONFIDENCE_CAP,
    derive_arc,
    derived_confidence,
    major_moves,
)


@pytest.mark.parametrize(
    ("points", "label"),
    [
        ([-0.5, -0.4, -0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3, 0.4, 0.5], "rags_to_riches"),
        ([0.5, 0.4, 0.3, 0.2, 0.1, 0.0, -0.1, -0.2, -0.3, -0.4, -0.5], "riches_to_rags"),
        ([0.2, 0.0, -0.3, -0.6, -0.5, -0.2, 0.1, 0.3, 0.5, 0.6, 0.7], "man_in_a_hole"),
        ([-0.2, 0.1, 0.4, 0.6, 0.7, 0.5, 0.2, -0.1, -0.4, -0.6, -0.7], "icarus"),
        ([-0.5, -0.1, 0.3, 0.4, 0.0, -0.4, -0.3, 0.0, 0.4, 0.7, 0.8], "cinderella"),
        ([0.5, 0.1, -0.3, -0.4, 0.0, 0.4, 0.3, 0.0, -0.4, -0.7, -0.8], "oedipus"),
        # wobbles under 0.3 are ignored
        ([0.0, -0.2, -0.5, -0.4, -0.3, -0.4, -0.4, -0.5, -0.6, -0.8, 0.9], "man_in_a_hole"),
    ],
)
def test_six_shapes(points: list[float], label: str) -> None:
    d = derive_arc(points)
    assert d.label == label
    assert d.threshold_used == 0.3
    assert not d.net_change_fallback


def test_move_of_exactly_threshold_counts_despite_float_error() -> None:
    assert 0.7 - 0.4 < 0.3  # the float bug QA found
    assert major_moves([0.4, 0.7]) == ("up",)
    assert major_moves([0.7, 0.4]) == ("down",)
    # peak 0.7, then back to 0.4: exactly 0.3 counts as a second move
    assert major_moves([0.1, 0.4, 0.7, 0.4]) == ("up", "down")


def test_move_just_under_threshold_does_not_count() -> None:
    assert major_moves([0.4, 0.69]) == ()
    assert major_moves([0.1, 0.4, 0.7, 0.41]) == ("up",)


@pytest.mark.parametrize(
    ("points", "label"),
    [
        ([0.0, 0.05, 0.1, 0.1, 0.15, 0.2, 0.2, 0.2, 0.25, 0.2, 0.2], "rags_to_riches"),
        ([0.2, 0.2, 0.15, 0.1, 0.1, 0.05, 0.0, 0.0, -0.05, 0.0, 0.0], "riches_to_rags"),
        ([0.1] * 11, "riches_to_rags"),  # exactly zero net change counts as a fall
    ],
)
def test_flat_story_falls_back_to_net_change(points: list[float], label: str) -> None:
    d = derive_arc(points)
    assert d.label == label
    assert d.net_change_fallback
    assert not d.reduced_shape
    assert d.legs == ()
    assert derived_confidence(0.9, d) == FALLBACK_CONFIDENCE_CAP
    assert derived_confidence(0.3, d) == 0.3


def test_four_or_more_moves_raise_threshold_until_three_or_fewer() -> None:
    # down, up, down, up at 0.3; the middle wiggle is 0.4 so it vanishes at 0.5
    points = [0.1, -0.2, -0.4, -0.3, 0.0, -0.5, -0.8, -0.6, -0.1, 0.5, 0.9]
    assert len(major_moves(points, 0.3)) == 4
    d = derive_arc(points)
    assert (d.label, d.threshold_used, d.legs) == ("man_in_a_hole", 0.5, ("down", "up"))
    assert not d.net_change_fallback


def test_long_series_zigzag_reduces_to_dominant_shape() -> None:
    # A series that climbs through many ups and downs, then collapses at the end.
    points = [-0.6, -0.2, -0.5, 0.0, -0.3, 0.3, 0.0, 0.6, 0.3, 0.8, -0.9]
    assert len(major_moves(points, 0.3)) >= 4
    d = derive_arc(points)
    assert d.label == "icarus"
    assert d.threshold_used == 0.4
    assert not d.reduced_shape


def test_oscillation_with_no_large_scale_shape_falls_back() -> None:
    points = [0.0, 0.35, 0.0, 0.35, 0.0, 0.35, 0.0, 0.35, 0.0, 0.35, 0.1]
    d = derive_arc(points)
    assert d.net_change_fallback
    assert d.label == "rags_to_riches"


def test_confidence_not_capped_without_fallback() -> None:
    d = derive_arc([0.2, 0.0, -0.3, -0.6, -0.5, -0.2, 0.1, 0.3, 0.5, 0.6, 0.7])
    assert derived_confidence(0.9, d) == 0.9


@pytest.mark.parametrize("points", [[0.0] * 10, [0.0] * 12, [1.5] + [0.0] * 10])
def test_invalid_points_raise(points: list[float]) -> None:
    with pytest.raises(ValueError):
        derive_arc(points)


W_SHAPE = [0.2, -0.4, -0.5, 0.2, 0.3, -0.3, -0.4, 0.1, 0.5, 0.6, 0.7]
M_SHAPE = [-p for p in W_SHAPE]


@pytest.mark.parametrize(
    ("points", "label"), [(W_SHAPE, "rags_to_riches"), (M_SHAPE, "riches_to_rags")], ids=["W", "M"]
)
def test_w_and_m_collapse_to_one_leg_and_are_flagged(points: list[float], label: str) -> None:
    assert len(major_moves(points, 0.3)) >= 4
    d = derive_arc(points)
    assert (d.label, d.threshold_used, len(d.legs)) == (label, 0.8, 1)
    assert d.reduced_shape and not d.net_change_fallback and d.unreliable
    assert derived_confidence(0.95, d) == FALLBACK_CONFIDENCE_CAP


def test_reduction_needing_threshold_0_6_is_flagged_even_with_two_legs() -> None:
    points = [0.0, -0.8, -0.3, -0.8, -0.3, -0.8, -0.3, 0.2, 0.5, 0.8, 0.8]
    assert len(major_moves(points, 0.5)) >= 4
    d = derive_arc(points)
    assert (d.label, d.threshold_used) == ("man_in_a_hole", 0.6)
    assert d.reduced_shape


def test_small_reductions_are_not_flagged() -> None:
    d = derive_arc([0.1, -0.2, -0.4, -0.3, 0.0, -0.5, -0.8, -0.6, -0.1, 0.5, 0.9])
    assert d.threshold_used == 0.5 and len(d.legs) == 2
    assert not d.reduced_shape and not d.unreliable


def test_full_range_oscillation_stops_at_threshold_2() -> None:
    points = [1.0, -1.0] * 5 + [1.0]
    assert len(major_moves(points, 2.0)) >= 4  # moves of exactly 2.0 still count
    d = derive_arc(points)
    assert d.threshold_used == 2.0  # schema maximum; never 2.1
    assert d.net_change_fallback and d.label == "riches_to_rags"  # net change is zero


def test_move_detection_is_symmetric_in_time_and_sign() -> None:
    """The opening sub-threshold move is dropped exactly like the closing one."""
    flip = {"up": "down", "down": "up"}
    rng = random.Random(20260926)
    for _ in range(3000):
        points = [round(rng.uniform(-1, 1), 1) for _ in range(11)]
        for threshold in (0.3, 0.5, 0.8, 1.2):
            moves = major_moves(points, threshold)
            reversed_moves = tuple(flip[m] for m in reversed(moves))
            assert major_moves(points[::-1], threshold) == reversed_moves
            assert major_moves([-p for p in points], threshold) == tuple(flip[m] for m in moves)
