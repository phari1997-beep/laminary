"""Gold tooling: sheet columns, template, importer round trip and errors, selector, pairs."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest
from wikimedia_fake import FakeWikimedia

from laminary_pipeline.annotation import load_schema, validate_record
from laminary_pipeline.evaluate.report import index_gold
from laminary_pipeline.gold import columns as col
from laminary_pipeline.gold.__main__ import main as gold_main
from laminary_pipeline.gold.importer import import_csv_text
from laminary_pipeline.gold.pairs import load_pairs, resolve_pairs
from laminary_pipeline.gold.select import select_gold, summarize_gold
from laminary_pipeline.gold.template import lists_csv, readme_csv, template_csv, template_rows
from laminary_pipeline.ingest.http import HttpClient
from laminary_pipeline.ingest.paths import DEFAULT_DATA_DIR, read_jsonl, write_jsonl_atomic
from laminary_pipeline.ingest.wikipedia import PlotFetcher

STAMP = "2026-10-01T09:00:00Z"
SHA = "a" * 64

# ---------- two hand-made rows ----------

PREFILLED_MOVIE = {
    "qid": "Q9000001", "title": "The Lantern Keeper", "year": "1994", "type": "movie",
    "wikipedia_revision_link":
        "https://en.wikipedia.org/w/index.php?title=The_Lantern_Keeper&oldid=1200000001",
    "plot_section": "Plot", "word_count": "197", "series_status": "", "tmdb_id": "90001",
    "source_ref": "https://en.wikipedia.org/wiki/The_Lantern_Keeper",
    "source_revision": "1200000001", "source_retrieved_at": "2026-09-30T12:00:00Z",
    "source_license": "CC-BY-SA-4.0", "source_word_count": "197", "source_sha256": SHA,
    "guide_version": "1.0.0",
}
PREFILLED_TV = {
    **PREFILLED_MOVIE,
    "qid": "Q9000003", "title": "Harbor Lights", "year": "2013", "type": "tv_series",
    "series_status": "ended", "tmdb_id": "90003", "source_revision": "1000000003",
    "source_ref": "https://en.wikipedia.org/wiki/Harbor_Lights_(TV_series)",
    "source_license": "CC-BY-SA-3.0", "word_count": "195", "source_word_count": "195",
}


def yn(present: set[str], keys: list[str], prefix: str) -> dict[str, str]:
    return {f"{prefix}{k}": ("Y" if k in present else "N") for k in keys}


# Row 1: a movie labeled with a shape from the dropdown, display names, confidence and notes.
MOVIE_LABELS = {
    "labeler_id": "L01",
    "primary_plot": "Rebirth",
    **yn({"rebirth", "voyage_and_return"}, col.BOOKER_PLOTS, "plot_"),
    "blueprint": "Hero's Journey",
    **yn({"ordinary_world", "call_to_adventure", "tests_allies_enemies", "ordeal",
          "return_with_the_elixir"}, col.STAGES, "stage_"),
    "arc_shape": "Man in a Hole (fall then rise)",
    **yn({"found_family"}, col.TAGS, "tag_"),
    "confidence": "Medium",
    "notes": "Clear rebirth engine; voyage is secondary.",
}
# Row 2: a series labeled with 11 arc points instead of a shape (lower-case y/n, schema keys).
TV_POINTS = ["0.2", "-0.1", "-0.3", "-0.5", "-0.2", "0.1", "0.3", "-0.1", "-0.4", "-0.6", "-0.3"]
TV_LABELS = {
    "labeler_id": "L02",
    "primary_plot": "mystery",
    **{k: v.lower() for k, v in yn({"mystery", "tragedy"}, col.BOOKER_PLOTS, "plot_").items()},
    "blueprint": "no_clear_blueprint",
    **{k: v.lower() for k, v in yn({"ordinary_world", "call_to_adventure"}, col.STAGES,
                                   "stage_").items()},
    **dict(zip(col.ARC_POINT_COLS, TV_POINTS, strict=True)),
    **yn({"ensemble_convergence", "pyrrhic_victory"}, col.TAGS, "tag_"),
}


def sheet(*rows: dict[str, str], columns: list[str] | None = None, help_row: bool = True) -> str:
    cols = columns or col.ALL_COLUMNS
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(cols)
    if help_row:
        h = col.help_row()
        w.writerow([h.get(c, "") for c in cols])
    for r in rows:
        w.writerow([r.get(c, "") for c in cols])
    return out.getvalue()


def test_two_hand_made_rows_round_trip_to_valid_gold_records() -> None:
    unlabeled = dict(PREFILLED_MOVIE, qid="Q9000009", title="Not yet labeled")
    result = import_csv_text(sheet({**PREFILLED_MOVIE, **MOVIE_LABELS},
                                   {**PREFILLED_TV, **TV_LABELS}, unlabeled),
                             annotated_at=STAMP)
    assert result.errors == [], "\n".join(map(str, result.errors))
    assert result.skipped_unlabeled == 1
    movie, tv = result.records
    for rec in (movie, tv):
        assert rec["record_kind"] == "gold_label"
        assert validate_record(rec) == []

    assert movie["title"] == {"media_type": "movie", "name": "The Lantern Keeper",
                              "release_year": 1994, "tmdb_id": 90001, "wikidata_id": "Q9000001"}
    prov = movie["provenance"]
    assert prov["annotator"] == {"labeler_id": "L01", "guide_version": "1.0.0"}
    assert prov["input_word_count"] == 197 and prov["annotated_at"] == STAMP
    assert prov["sources"][0]["kind"] == "wikipedia_plot"
    assert prov["notes"].startswith("Clear rebirth")
    plot = movie["layers"]["archetypal_plot"]
    assert plot["primary"] == {"label": "rebirth", "confidence": 0.8}
    assert [k for k, v in plot["plots"].items() if v["present"]] == ["voyage_and_return", "rebirth"]
    assert movie["layers"]["mythic_blueprint"]["blueprint"] == {"label": "heros_journey"}
    assert movie["layers"]["structural_skeleton"] == {"emotional_arc": {
        "label": "man_in_a_hole", "method": "labeler_assigned", "net_change_fallback": False}}
    assert movie["beat_tags"]["tags"]["found_family"] == {"present": True}
    assert len(movie["beat_tags"]["tags"]) == 10

    assert tv["title"]["series_status"] == "ended"
    skel = tv["layers"]["structural_skeleton"]
    assert skel["arc_points"] == [float(p) for p in TV_POINTS]
    assert skel["emotional_arc"]["method"] == "derived"
    assert skel["emotional_arc"]["label"] == "oedipus"
    assert tv["layers"]["archetypal_plot"]["primary"] == {"label": "mystery"}


def test_flat_story_uses_flagged_net_change_label() -> None:
    row = {**PREFILLED_MOVIE, **MOVIE_LABELS, "arc_shape": "Flat: ends better than it starts"}
    rec = import_csv_text(sheet(row), annotated_at=STAMP).records[0]
    assert rec["layers"]["structural_skeleton"]["emotional_arc"] == {
        "label": "rags_to_riches", "method": "labeler_assigned", "net_change_fallback": True}
    down = {**row, "arc_shape": "Flat: ends the same or worse"}
    rec = import_csv_text(sheet(down), annotated_at=STAMP).records[0]
    assert rec["layers"]["structural_skeleton"]["emotional_arc"]["label"] == "riches_to_rags"


def test_shape_and_points_disagreeing_is_a_warning() -> None:
    row = {**PREFILLED_MOVIE, **MOVIE_LABELS, **dict(zip(col.ARC_POINT_COLS, TV_POINTS,
                                                         strict=True))}
    result = import_csv_text(sheet(row), annotated_at=STAMP)
    assert result.ok and "trace oedipus" in str(result.warnings[0])
    skel = result.records[0]["layers"]["structural_skeleton"]
    assert skel["emotional_arc"]["label"] == "man_in_a_hole" and len(skel["arc_points"]) == 11


def test_skip_reason_gives_abstained_record() -> None:
    row = {**PREFILLED_MOVIE, "labeler_id": "L01", "skip_reason": "Summary too thin"}
    result = import_csv_text(sheet(row), annotated_at=STAMP)
    rec = result.records[0]
    assert rec["outcome"] == "abstained" and rec["abstain_reason"] == "summary_too_thin"
    assert "layers" not in rec and validate_record(rec) == []


def test_google_sheets_export_quirks() -> None:
    """BOM, CRLF line endings, reordered and extra columns, header case and spacing."""
    cols = list(reversed(col.ALL_COLUMNS)) + ["Extra column"]
    text = sheet({**PREFILLED_MOVIE, **MOVIE_LABELS}, columns=cols)
    header, rest = text.split("\n", 1)
    header = header.replace("labeler_id", " Labeler_ID ")
    text = "﻿" + (header + "\n" + rest).replace("\n", "\r\n")
    result = import_csv_text(text, annotated_at=STAMP)
    assert result.ok, "\n".join(map(str, result.errors))
    assert validate_record(result.records[0]) == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"plot_comedy": "", "tag_time_loop": ""}, "blank: plot_comedy"),
        ({"plot_comedy": "maybe"}, "only Y or N allowed; got plot_comedy='maybe'"),
        ({"plot_rebirth": "N"}, "the primary plot must be marked Y in plot_rebirth"),
        ({"plot_comedy": "Y", "plot_mystery": "Y"}, "at most 2 plots besides the primary"),
        ({"primary_plot": "Heist"}, "primary_plot 'Heist' is not one of"),
        ({"primary_plot": ""}, "primary_plot is blank"),
        ({"blueprint": "Villain journey"}, "blueprint 'Villain journey' is not one of"),
        ({"arc_shape": "Spiral"}, "arc_shape 'Spiral' is not one of"),
        ({"arc_shape": "", "arc_t00": "0.1"}, "fill all 11 or none"),
        ({"arc_shape": ""}, "choose an arc_shape, or fill all 11 arc points"),
        ({"arc_shape": "", **{c: "2" for c in col.ARC_POINT_COLS}}, "between -1 and 1"),
        ({"confidence": "Very"}, "confidence 'Very' is not one of"),
        ({"labeler_id": ""}, "labeler_id is blank"),
        ({"tmdb_id": ""}, "no TMDB id in Wikidata"),
        ({"type": "tv_series"}, "series_status must be 'ended', 'ongoing' or 'unknown'"),
        ({"skip_reason": "Too long"}, "skip_reason 'Too long' is not one of"),
    ],
)
def test_row_errors_are_clear(change: dict[str, str], message: str) -> None:
    row = {**PREFILLED_MOVIE, **MOVIE_LABELS, **change}
    result = import_csv_text(sheet(row), annotated_at=STAMP)
    assert not result.ok and result.records == []
    text = str(result.errors[0])
    assert text.startswith("Row 3 (The Lantern Keeper):"), text
    assert message in text, text


def test_duplicate_labeler_for_same_title_is_an_error_but_two_labelers_are_fine() -> None:
    a = {**PREFILLED_MOVIE, **MOVIE_LABELS, "label_slot": "1"}
    b = {**a, "labeler_id": "L02", "label_slot": "2"}
    # slot 2 on an earlier sheet row still comes out after slot 1 (the evaluation's reference)
    result = import_csv_text(sheet(b, a), annotated_at=STAMP)
    assert result.ok, "\n".join(map(str, result.errors))
    labelers = [r["provenance"]["annotator"]["labeler_id"] for r in result.records]
    assert labelers == ["L01", "L02"]
    gold = index_gold(result.records)
    assert gold.reference["movie:90001"]["provenance"]["annotator"]["labeler_id"] == "L01"
    assert gold.second["movie:90001"]["provenance"]["annotator"]["labeler_id"] == "L02"
    dup = import_csv_text(sheet(a, {**a, "label_slot": "2"}), annotated_at=STAMP)
    assert "already labeled Q9000001 on row 3" in str(dup.errors[0])


@pytest.mark.parametrize(
    "slots,message",
    [(("1", "1"), "label_slot 1 of Q9000001 is already labeled on row 3"),
     (("1", "3"), "label_slot '3' must be 1 or 2")],
)
def test_label_slot_errors(slots: tuple[str, str], message: str) -> None:
    a = {**PREFILLED_MOVIE, **MOVIE_LABELS, "label_slot": slots[0]}
    b = {**a, "labeler_id": "L02", "label_slot": slots[1]}
    result = import_csv_text(sheet(a, b), annotated_at=STAMP)
    assert len(result.records) == 1 and message in str(result.errors[0])


def test_blank_label_slot_means_slot_one() -> None:
    a = {**PREFILLED_MOVIE, **MOVIE_LABELS}
    b = {**a, "labeler_id": "L02", "label_slot": "2"}
    assert len(import_csv_text(sheet(a, b), annotated_at=STAMP).records) == 2


def test_schema_check_catches_bad_prefilled_source() -> None:
    row = {**PREFILLED_MOVIE, **MOVIE_LABELS, "source_ref": "tmdb:movie/603",
           "source_license": "TMDB-API-terms"}
    result = import_csv_text(sheet(row), annotated_at=STAMP)
    assert not result.ok and "schema check" in str(result.errors[0])


# ---------- columns, template ----------


def test_columns_cover_exactly_the_gold_label_fields() -> None:
    schema = load_schema()["$defs"]
    assert len(col.PLOT_COLS) == 9 and len(col.STAGE_COLS) == 12 and len(col.TAG_COLS) == 10
    assert len(col.ARC_POINT_COLS) == 11
    assert set(col.PLOT_NAMES) == set(schema["booker_plot"]["enum"])
    assert set(col.BLUEPRINT_NAMES) == set(schema["mythic_blueprint"]["enum"])
    assert set(col.STAGE_NAMES) == set(schema["journey_stage"]["enum"])
    assert set(col.ARC_NAMES) == set(schema["emotional_arc"]["enum"])
    assert set(col.TAG_NAMES) == set(schema["beat_tag"]["enum"])
    assert set(col.SKIP_NAMES) == set(schema["abstain_reason"]["enum"])
    assert len(col.ALL_COLUMNS) == len(set(col.ALL_COLUMNS)) == 67
    for c in col.LABELER_COLS:
        if c.startswith(("plot_", "stage_", "tag_")):
            assert col.dropdown_for_column(c) == "yes_no"
    for name in col.dropdown_lists():
        assert name == "yes_no" or col.dropdown_for_column(name) == name


def test_template_from_plot_files_round_trips(tmp_path: Path) -> None:
    fetcher = PlotFetcher(HttpClient(FakeWikimedia(), None, min_interval={}),
                          clock=lambda: "2026-09-30T12:00:00Z")
    plots = {
        "Q9000001": fetcher.fetch({"qid": "Q9000001", "enwiki_title": "The Lantern Keeper",
                                   "media_type": "movie", "title": "The Lantern Keeper"}),
        "Q9000003": fetcher.fetch({"qid": "Q9000003", "enwiki_title": "Harbor Lights (TV series)",
                                   "media_type": "tv_series", "series_status": "ended"}),
        "Q9000002": fetcher.fetch({"qid": "Q9000002", "enwiki_title": "Nizhal Veedu",
                                   "media_type": "movie"}),
    }
    selection = [
        {"qid": "Q9000001", "title": "The Lantern Keeper", "year": 1994, "media_type": "movie",
         "tmdb_id": 90001, "guessed_plot": "rebirth"},
        {"qid": "Q9000003", "title": "Harbor Lights", "year": 2013, "media_type": "tv_series",
         "tmdb_id": 90003, "guessed_plot": "mystery", "double_label": True},
        {"qid": "Q9000002", "title": "Nizhal Veedu", "year": 2003, "media_type": "movie",
         "tmdb_id": 90002, "guessed_plot": "comedy"},
    ]
    rows, skipped = template_rows(selection, plots)
    assert [(r["qid"], r["label_slot"]) for r in rows] == [
        ("Q9000001", "1"), ("Q9000003", "1"), ("Q9000003", "2")
    ]
    assert rows[1] == {**rows[2], "label_slot": "1"}  # the two slots differ only in the slot
    assert "Q9000002" in skipped[0]
    text = template_csv(rows)
    lines = list(csv.reader(io.StringIO(text)))
    assert lines[0] == col.ALL_COLUMNS and lines[1][0].startswith("#")
    assert "guessed" not in text and "rebirth" not in text.split("\n", 2)[2]  # guesses hidden
    assert rows[0]["wikipedia_revision_link"].endswith("oldid=1200000001")
    assert rows[1]["series_status"] == "ended"
    assert rows[0]["summary_text_file"] == "texts/Q9000001.txt"
    # a plot whose text doesn't match its hash is left out of the sheet
    tampered = {**plots["Q9000001"], "text": plots["Q9000001"]["text"] + " extra"}
    rows_t, skipped_t = template_rows(selection[:1], {"Q9000001": tampered})
    assert rows_t == [] and "sha256" in skipped_t[0]

    parsed = list(csv.DictReader(io.StringIO(text)))[1:]  # drop help row
    parsed[0].update(MOVIE_LABELS)
    parsed[1].update(TV_LABELS)
    parsed[2].update(TV_LABELS, labeler_id="L03")
    result = import_csv_text(sheet(*parsed), annotated_at=STAMP)
    assert result.ok, "\n".join(map(str, result.errors))
    assert [validate_record(r) for r in result.records] == [[], [], []]
    assert result.records[0]["provenance"]["sources"][0] == plots["Q9000001"]["source"]


def test_lists_and_readme_tabs() -> None:
    lists = list(csv.reader(io.StringIO(lists_csv())))
    assert lists[0] == list(col.dropdown_lists())
    arc_col = [r[lists[0].index("arc_shape")] for r in lists[1:]]
    assert col.FLAT_UP in arc_col and "Oedipus (fall, rise, fall)" in arc_col
    readme = readme_csv()
    assert "GOLD_LABELING_GUIDE" in readme and "primary_plot: dropdown primary_plot" in readme


# ---------- selector ----------


def cand(i: int, media_type: str, region: str, seed: str | None, **kw: Any) -> dict[str, Any]:
    return {"qid": f"Q{i}", "title": seed or f"T{i}", "year": 2000, "media_type": media_type,
            "region": region, "bucket": "x", "sitelinks": 1000 - i, "tmdb_id": i,
            "tmdb_id_ambiguous": False, "gold_seed_title": seed, "role": "pilot", **kw}


def test_gold_selector_balances_type_region_and_plots() -> None:
    plots = col.BOOKER_PLOTS
    arcs = [*col.ARCS, "flat"]
    seeds, cands = [], []
    for i in range(160):
        region = "english" if i % 3 else ["tamil", "korean", "japanese", "hindi"][i % 4]
        mt = "tv_series" if i % 4 == 0 else "movie"
        title = f"Seed {i}"
        seeds.append({"title": title, "guessed_plot": plots[i % len(plots)],
                      "guessed_arc": arcs[i % len(arcs)]})
        cands.append(cand(i + 1, mt, region, title))
    cands.append(cand(999, "movie", "english", None, tmdb_id=None))  # no TMDB id
    cands.append(cand(998, "movie", "english", None))  # plot failed
    rows = select_gold(cands, seeds, plot_ok=lambda q: q != "Q998")
    s = summarize_gold(rows)
    assert s["total"] == 100 and s["seeds"] == 100
    assert s["by_media_type"] == {"movie": 70, "tv_series": 30}
    assert 100 - s["by_region"].get("english", 0) >= 20
    counts = s["by_guessed_plot"].values()
    assert len(s["by_guessed_plot"]) == 9 and max(counts) - min(counts) <= 3
    assert not {"Q999", "Q998"} & {r["qid"] for r in rows}
    # double labeling: 25 titles spread through the pick order, both types represented
    assert s["double_labeled"] == 25 and sum(r["double_label"] for r in rows) == 25
    assert set(s["double_labeled_by_media_type"]) == {"movie", "tv_series"}
    assert [i for i, r in enumerate(rows) if r["double_label"]] == list(range(0, 100, 4))


def test_gold_selector_tops_up_with_famous_non_seeds() -> None:
    cands = [cand(i, "movie", "english", None) for i in range(1, 11)]
    rows = select_gold(cands, [], n=5, tv_share=0)
    assert [r["qid"] for r in rows] == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    assert all(r["guessed_plot"] == "unknown" for r in rows)
    assert all(r["double_label"] for r in rows)  # fewer titles than DOUBLE_LABEL_N: all
    assert not any(r["double_label"] for r in select_gold(cands, [], n=5, double_label_n=0))


# ---------- pairs ----------


def test_similarity_pairs_config_is_valid() -> None:
    pairs, errors = load_pairs(DEFAULT_DATA_DIR / "config" / "similarity_pairs.csv")
    assert errors == []
    no_match = [p for p in pairs if p.expect == "no_match"]
    match = [p for p in pairs if p.expect == "match"]
    assert len(no_match) >= 20 and len(match) >= 20
    first = no_match[0]
    assert (first.pair_id, first.a[0], first.b[0]) == ("N01", "Finding Nemo", "Taken")
    resolved = resolve_pairs(pairs, [
        {"qid": "Q1", "title": "Finding Nemo", "year": 2003, "media_type": "movie"},
        {"qid": "Q2", "title": "Taken", "year": 2008, "media_type": "movie"},
    ])
    assert resolved[0]["qid_a"] == "Q1" and resolved[0]["qid_b"] == "Q2"


def test_gold_seed_config_is_well_formed() -> None:
    path = DEFAULT_DATA_DIR / "config" / "gold_seed_titles.csv"
    with path.open(encoding="utf-8") as fh:
        seeds = list(csv.DictReader(fh))
    assert len(seeds) >= 120
    keys = {(s["title"], s["year"], s["media_type"]) for s in seeds}
    assert len(keys) == len(seeds)
    for s in seeds:
        assert s["media_type"] in ("movie", "tv_series") and s["year"].isdigit()
        assert s["guessed_plot"] in col.BOOKER_PLOTS, s
        assert s["guessed_arc"] in [*col.ARCS, "flat"], s
    assert {s["guessed_plot"] for s in seeds} == set(col.BOOKER_PLOTS)
    assert sum(s["region"] != "english" for s in seeds) >= 40
    assert sum(s["media_type"] == "tv_series" for s in seeds) >= 40


# ---------- CLI ----------


def test_gold_cli_select_template_import(tmp_path: Path) -> None:
    data = tmp_path / "data"
    fetcher = PlotFetcher(HttpClient(FakeWikimedia(), None, min_interval={}),
                          clock=lambda: "2026-09-30T12:00:00Z")
    candidates = [
        {**cand(9000001, "movie", "english", "The Lantern Keeper"), "qid": "Q9000001",
         "enwiki_title": "The Lantern Keeper", "bucket": "film:english", "bucket_rank": 1},
        {**cand(9000002, "movie", "tamil", None), "qid": "Q9000002",
         "enwiki_title": "Nizhal Veedu", "bucket": "film:tamil", "bucket_rank": 1},
    ]
    write_jsonl_atomic(data / "pilot_candidates.jsonl", candidates)
    for c in candidates:
        rec = fetcher.fetch(c)
        (data / "plots").mkdir(parents=True, exist_ok=True)
        (data / "plots" / f"{c['qid']}.json").write_text(json.dumps(rec))
    log: list[str] = []
    assert gold_main(["--data-dir", str(data), "select", "--n", "5"], log=log.append) == 0
    selection = list(read_jsonl(data / "gold" / "gold_selection.jsonl"))
    assert [s["qid"] for s in selection] == ["Q9000001"]  # the too-short title is excluded
    assert gold_main(["--data-dir", str(data), "template"], log=log.append) == 0
    template = (data / "gold" / "gold_labels_template.csv").read_text()
    assert (data / "gold" / "gold_labels_lists.csv").exists()
    assert (data / "gold" / "gold_labels_readme.csv").exists()
    # M3: the labeler reads the exact text the model sees, byte for byte
    plot = json.loads((data / "plots" / "Q9000001.json").read_text())
    text_file = data / "gold" / "texts" / "Q9000001.txt"
    assert text_file.read_bytes() == plot["text"].encode("utf-8")
    assert hashlib.sha256(text_file.read_bytes()).hexdigest() == plot["source"]["content_sha256"]
    assert "texts/Q9000001.txt" in template

    filled = list(csv.DictReader(io.StringIO(template)))[1:]
    filled[0].update(MOVIE_LABELS)
    sheet_path = tmp_path / "filled.csv"
    sheet_path.write_text(sheet(*filled), encoding="utf-8")
    code = gold_main(["--data-dir", str(data), "import", str(sheet_path), "--labeled-at", STAMP],
                     log=log.append)
    assert code == 0, "\n".join(log)
    records = list(read_jsonl(data / "gold" / "gold_labels.jsonl"))
    assert len(records) == 1 and validate_record(records[0]) == []

    bad = tmp_path / "bad.csv"
    bad.write_text(sheet({**filled[0], "plot_rebirth": "N"}), encoding="utf-8")
    out = tmp_path / "out.jsonl"
    code = gold_main(["--data-dir", str(data), "import", str(bad), "--out", str(out)],
                     log=log.append)
    assert code == 1 and not out.exists()
    assert gold_main(["--data-dir", str(data), "pairs"], log=log.append) == 1  # no config here
