"""Gold sheet layout: columns, dropdown values and how each maps to the schema.

Vocabulary keys come from the packaged annotation schema, so the sheet can't drift from it.
Display names follow docs/NARRATIVE_SCHEMA.md. The importer accepts either the display name or
the schema key, case-insensitively.
"""

from __future__ import annotations

from laminary_pipeline.annotation import load_schema

# Version of docs/GOLD_LABELING_GUIDE.md. Stored as provenance.annotator.guide_version.
GUIDE_VERSION = "1.4.0"  # 1.4.0: summary_coverage column, partial-coverage rule


def _enum(name: str) -> list[str]:
    return list(load_schema()["$defs"][name]["enum"])


BOOKER_PLOTS = _enum("booker_plot")
BLUEPRINTS = _enum("mythic_blueprint")
STAGES = _enum("journey_stage")
ARCS = _enum("emotional_arc")
TAGS = _enum("beat_tag")
ABSTAIN_REASONS = _enum("abstain_reason")
ARC_POINT_COUNT = 11

PLOT_NAMES = {
    "overcoming_the_monster": "Overcoming the Monster",
    "rags_to_riches": "Rags to Riches",
    "the_quest": "The Quest",
    "voyage_and_return": "Voyage and Return",
    "comedy": "Comedy",
    "tragedy": "Tragedy",
    "rebirth": "Rebirth",
    "rebellion_against_the_one": "Rebellion Against the One",
    "mystery": "Mystery",
}
BLUEPRINT_NAMES = {
    "heros_journey": "Hero's Journey",
    "heroines_journey": "Heroine's Journey",
    "anti_hero_descent": "Anti-hero descent",
    "no_clear_blueprint": "No clear blueprint",
}
STAGE_NAMES = {
    "ordinary_world": "Ordinary World",
    "call_to_adventure": "Call to Adventure",
    "refusal_of_the_call": "Refusal of the Call",
    "meeting_with_the_mentor": "Meeting with the Mentor",
    "crossing_the_first_threshold": "Crossing the First Threshold",
    "tests_allies_enemies": "Tests, Allies, Enemies",
    "approach_to_the_inmost_cave": "Approach to the Inmost Cave",
    "ordeal": "Ordeal",
    "reward": "Reward",
    "the_road_back": "The Road Back",
    "resurrection": "Resurrection",
    "return_with_the_elixir": "Return with the Elixir",
}
ARC_NAMES = {
    "rags_to_riches": "Rags to Riches (steady rise)",
    "riches_to_rags": "Riches to Rags (steady fall)",
    "man_in_a_hole": "Man in a Hole (fall then rise)",
    "icarus": "Icarus (rise then fall)",
    "cinderella": "Cinderella (rise, fall, rise)",
    "oedipus": "Oedipus (fall, rise, fall)",
}
# Flat stories (no major rise or fall): the net-change placeholder, flagged (DECISIONS
# 2026-09-29; NARRATIVE_SCHEMA section 8 "Gold guide for flat stories").
FLAT_UP = "Flat: ends better than it starts"
FLAT_DOWN = "Flat: ends the same or worse"
FLAT_ARCS = {FLAT_UP: "rags_to_riches", FLAT_DOWN: "riches_to_rags"}
TAG_NAMES = {
    "mentor_dies": "Mentor dies",
    "twist_ending": "Twist ending",
    "unreliable_narrator": "Unreliable narrator",
    "found_family": "Found family",
    "redemption_arc": "Redemption arc",
    "pyrrhic_victory": "Pyrrhic victory",
    "time_loop": "Time loop",
    "heist_structure": "Heist structure",
    "ensemble_convergence": "Ensemble convergence",
    "ambiguous_ending": "Ambiguous ending",
}
SKIP_NAMES = {
    "summary_too_thin": "Summary too thin",
    "summary_contradictory": "Summary contradictory",
    "not_a_narrative": "Not a narrative",
    "summary_title_mismatch": "Summary is about a different work",
}
# Labeler confidence in the primary plot -> numeric (midpoints of the schema's bands, section 4).
CONFIDENCE_VALUES = {"High": 0.95, "Medium": 0.8, "Low": 0.6, "Weak": 0.4}
YES = frozenset({"y", "yes", "true", "1", "present"})
NO = frozenset({"n", "no", "false", "0", "absent"})

# ---------- columns ----------

PREFILLED_LEFT = [
    "qid", "title", "year", "type", "label_slot", "summary_text_file", "wikipedia_revision_link",
    "plot_section", "word_count", "summary_coverage",
]
PLOT_COLS = [f"plot_{k}" for k in BOOKER_PLOTS]
STAGE_COLS = [f"stage_{k}" for k in STAGES]
ARC_POINT_COLS = [f"arc_t{i:02d}" for i in range(ARC_POINT_COUNT)]
TAG_COLS = [f"tag_{k}" for k in TAGS]
LABELER_COLS = [
    "labeler_id", "skip_reason", "primary_plot", *PLOT_COLS, "blueprint", *STAGE_COLS,
    "arc_shape", *ARC_POINT_COLS, *TAG_COLS, "confidence", "notes",
]
PREFILLED_RIGHT = [
    "series_status", "tmdb_id", "source_ref", "source_revision", "source_retrieved_at",
    "source_license", "source_word_count", "source_sha256", "guide_version",
]
ALL_COLUMNS = [*PREFILLED_LEFT, *LABELER_COLS, *PREFILLED_RIGHT]
HELP_MARKER = "#"
# Double-labeled titles get two rows: slot 1 (the reference the model is scored against) and
# slot 2 (the second labeler). Other titles have one row, slot 1.
LABEL_SLOTS = ("1", "2")


def help_row() -> dict[str, str]:
    """Second header row: allowed values / meaning per column. The importer skips any row whose
    first cell starts with '#'."""
    h: dict[str, str] = {c: "(filled in; don't edit)" for c in PREFILLED_LEFT + PREFILLED_RIGHT}
    h["qid"] = "# HELP ROW: keep it; the importer skips rows starting with #"
    h["summary_text_file"] = (
        "READ THIS FILE ONLY: the exact summary text the model sees (sha256 = source_sha256)"
    )
    h["wikipedia_revision_link"] = (
        "Attribution only (CC BY-SA source). Don't label from the web page: it has extra "
        "tables, captions and notes the model never sees"
    )
    h["summary_coverage"] = (
        "If filled (e.g. 'Summary covers seasons 1–4 of 7.'), the summary stops before the "
        "series does: label only what it covers and don't judge an ending you can't see. The "
        "model gets the same line"
    )
    h["label_slot"] = (
        "1, or 2 for a title two people label. Take a slot Hari assigns; label on your own"
    )
    h["labeler_id"] = "Your labeler code, e.g. L01"
    h["skip_reason"] = "Leave blank unless you can't label: " + " / ".join(SKIP_NAMES.values())
    h["primary_plot"] = "One of: " + " / ".join(PLOT_NAMES.values())
    for key, col in zip(BOOKER_PLOTS, PLOT_COLS, strict=True):
        h[col] = f"Y/N: is '{PLOT_NAMES[key]}' a major thread? Primary must be Y; max 2 others"
    h["blueprint"] = "One of: " + " / ".join(BLUEPRINT_NAMES.values())
    for key, col in zip(STAGES, STAGE_COLS, strict=True):
        h[col] = f"Y/N: stage '{STAGE_NAMES[key]}'"
    h["arc_shape"] = (
        "One of the 6 shapes or a Flat option. Or leave blank and fill all 11 arc_t points"
    )
    for i, col in enumerate(ARC_POINT_COLS):
        h[col] = f"Optional: fortune at {i * 10}% of the story, -1 to 1"
    for key, col in zip(TAGS, TAG_COLS, strict=True):
        h[col] = f"Y/N: {TAG_NAMES[key]}"
    h["confidence"] = "How sure of the primary plot: " + " / ".join(CONFIDENCE_VALUES)
    h["notes"] = "Optional; internal only"
    return h


def dropdown_lists() -> dict[str, list[str]]:
    """Values for Google Sheets data validation ("Dropdown from a range" on the Lists tab)."""
    return {
        "yes_no": ["Y", "N"],
        "primary_plot": list(PLOT_NAMES.values()),
        "blueprint": list(BLUEPRINT_NAMES.values()),
        "arc_shape": [*ARC_NAMES.values(), FLAT_UP, FLAT_DOWN],
        "skip_reason": list(SKIP_NAMES.values()),
        "confidence": list(CONFIDENCE_VALUES),
    }


def dropdown_for_column(column: str) -> str | None:
    if column in PLOT_COLS or column in STAGE_COLS or column in TAG_COLS:
        return "yes_no"
    if column in ("primary_plot", "blueprint", "arc_shape", "skip_reason", "confidence"):
        return column
    return None
