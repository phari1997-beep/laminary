"""The similarity-pairs title set (``ingest pairs`` and ``plots --pairs``).

Resolution never guesses (ambiguous / unresolved / ineligible titles are reported and left
out), resolved rows have the candidate row shape with role "pairs", and neither command writes
the candidate list, the effective pilot or the pilot plot report.
"""

from __future__ import annotations

import json
import socket
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from annotate_support import ingest_plot
from wikimedia_fake import FakeWikimedia

from laminary_pipeline.evaluate.pairs import load_pairs as load_resolved
from laminary_pipeline.gold.pairs import Pair, resolve_pairs
from laminary_pipeline.ingest import pairs as ps
from laminary_pipeline.ingest.__main__ import main
from laminary_pipeline.ingest.paths import DataPaths, read_jsonl, write_json_atomic
from laminary_pipeline.ingest.plots import (
    effective_pilot,
    existing_status,
    not_annotatable,
    pairs_owned,
    plots_report,
    run_plots,
)

NOW = "2026-10-03T12:00:00Z"


def item(qid: str, title: str, year: int, media_type: str = "movie", **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "qid": qid, "title": title, "enwiki_title": title, "year": year, "end_year": None,
        "media_type": media_type, "series_status": None, "series_status_basis": None,
        "number_of_seasons": None, "tmdb_id": int(qid[1:]), "tmdb_id_ambiguous": False,
        "imdb_id": f"tt{qid[1:]}", "sitelinks": 10, "classes": [], "languages": ["Q1860"],
        "language_labels": {"Q1860": "English"}, "countries": ["Q30"],
        "genres": ["drama film"],
    }
    if media_type == "tv_series":
        base.update(series_status="ended", series_status_basis="P582", end_year=year + 3)
    return {**base, **kw}


def pair(pid: str, a: tuple[str, int, str], b: tuple[str, int, str],
         expect: str = "match") -> Pair:
    return Pair(pid, expect, a, b, "shape", "why", "proposed")


# ---------- unit ----------


def test_pair_titles_are_distinct_in_file_order() -> None:
    pairs = [pair("M1", ("A", 2000, "movie"), ("B", 2001, "movie")),
             pair("M2", ("B", 2001, "movie"), ("C", 2002, "tv_series"))]
    assert ps.pair_titles(pairs) == {("A", 2000, "movie"): ["M1"],
                                     ("B", 2001, "movie"): ["M1", "M2"],
                                     ("C", 2002, "tv_series"): ["M2"]}


def test_pick_prefers_exact_year_and_refuses_ties() -> None:
    key = ("Triangle", 2009, "movie")
    exact, near = item("Q1", "Triangle", 2009), item("Q2", "Triangle", 2010)
    assert ps.pick(key, [near, exact])[:2] == ("resolved", exact)
    twin = item("Q3", "Triangle", 2009, enwiki_title="Triangle (2009 South Korean film)")
    status, chosen, top = ps.pick(key, [exact, twin, near])
    assert status == "ambiguous" and chosen is None
    assert {t["qid"] for t in top} == {"Q1", "Q3"}  # the off-by-one item is not a contender
    assert ps.pick(key, [item("Q4", "Triangle", 2011)])[0] == "unresolved"  # 2 years off
    assert ps.pick(key, [item("Q5", "Triangle", 2009, media_type="tv_series")])[0] == (
        "unresolved")  # wrong type
    # a lone off-by-one hit resolves (the candidate rule) and is marked
    status, chosen, _ = ps.pick(key, [near])
    assert status == "resolved" and chosen is near


def test_a_pin_picks_among_matching_items_only() -> None:
    """N13 (DECISIONS 2026-10-03): the pairs file pins Triangle (2009) to the British film."""
    key = ("Triangle", 2009, "movie")
    uk = item("Q1783930", "Triangle", 2009, enwiki_title="Triangle (2009 British film)")
    kr = item("Q18648554", "Triangle", 2009, enwiki_title="Triangle (2009 South Korean film)")
    assert ps.pick(key, [uk, kr])[0] == "ambiguous"
    assert ps.pick(key, [uk, kr], "Q1783930")[:2] == ("resolved", uk)
    assert ps.pick(key, [uk, kr], "Q18648554")[:2] == ("resolved", kr)
    # a pin never overrides the title / type / year checks
    status, chosen, _ = ps.pick(key, [uk, kr, item("Q7", "Other", 2009)], "Q7")
    assert (status, chosen) == ("unresolved", None)
    pairs = [Pair("N13", "no_match", ("Palm Springs", 2020, "movie"), key, "s", "w",
                  "proposed", None, "Q1783930")]
    built = ps.build(pairs, [], {q["qid"]: q for q in (uk, kr)}, in_pilot=lambda q: False)
    tri = [t for t in built.titles if t.key == key][0]
    assert (tri.status, tri.qid) == ("resolved", "Q1783930")
    assert "pinned" in (tri.detail or "")
    assert built.rows[0]["qid"] == "Q1783930" and built.rows[0]["qid_pinned"] is True
    assert ps.to_look_up(pairs, []) == ([("Palm Springs", 2020, "movie"), key], ["Q1783930"])
    bad = [Pair("N13", "no_match", ("Palm Springs", 2020, "movie"), key, "s", "w",
                "proposed", None, "Q7")]
    built = ps.build(bad, [], {"Q7": item("Q7", "Other", 2009)}, in_pilot=lambda q: False)
    tri = [t for t in built.titles if t.key == key][0]
    assert tri.status == "unresolved" and "pinned QID Q7" in (tri.detail or "")


def test_a_pin_selects_the_candidate() -> None:
    a = {**item("Q1", "Triangle", 2009), "role": "pilot"}
    b = {**item("Q2", "Triangle", 2009), "role": "pilot"}
    pairs = [Pair("N1", "no_match", ("Triangle", 2009, "movie"), ("X", 2000, "movie"), "s",
                  "w", "proposed", "Q2")]
    assert resolve_pairs(pairs, [a, b])[0]["qid_a"] == "Q2"
    built = ps.build(pairs, [a, b], {}, in_pilot=lambda q: True)
    assert (built.titles[0].status, built.titles[0].qid) == ("in_pilot", "Q2")


def test_pick_matches_enwiki_title_and_aliases() -> None:
    grinch = item("Q131864", "Dr. Seuss' How the Grinch Stole Christmas", 2000,
                  enwiki_title="How the Grinch Stole Christmas (2000 film)")
    assert ps.pick(("How the Grinch Stole Christmas", 2000, "movie"), [grinch])[1] is grinch
    geass = item("Q4384067", "Code Geass Lelouch of the Rebellion", 2006, "tv_series",
                 seed_labels=["Code Geass"])
    assert ps.pick(("Code Geass", 2006, "tv_series"), [geass])[1] is geass


def test_build_classifies_every_title() -> None:
    cand_with_plot = {**item("Q10", "Finding Nemo", 2003), "role": "pilot", "bucket_rank": 3,
                      "bucket": "film:english"}
    cand_no_plot = {**item("Q11", "Home Alone", 1990), "role": "reserve", "bucket_rank": 35,
                    "bucket": "film:english"}
    pairs = [
        pair("N1", ("Finding Nemo", 2003, "movie"), ("Taken", 2008, "movie"), "no_match"),
        pair("N2", ("Home Alone", 1990, "movie"), ("Straw Dogs", 1971, "movie"), "no_match"),
        pair("N3", ("Triangle", 2009, "movie"), ("Ozark", 2017, "tv_series"), "no_match"),
        pair("M1", ("Nowhere", 2000, "movie"), ("Finding Nemo", 2003, "movie")),
    ]
    items = {
        "Q20": item("Q20", "Taken", 2008, tmdb_id=None),  # ineligible: no TMDB id
        "Q21": item("Q21", "Straw Dogs", 1971),
        "Q22": item("Q22", "Triangle", 2009),
        "Q23": item("Q23", "Triangle", 2009),
        "Q24": item("Q24", "Ozark", 2017, "tv_series", languages=["Q1860"]),
    }
    built = ps.build(pairs, [cand_with_plot, cand_no_plot], items,
                     in_pilot=lambda q: q == "Q10", retrieved_at=NOW)
    status = {t.key[0]: (t.status, t.qid) for t in built.titles}
    assert status == {
        "Finding Nemo": ("in_pilot", "Q10"),
        "Taken": ("ineligible", "Q20"),
        "Home Alone": ("candidate_outside_pilot", "Q11"),
        "Straw Dogs": ("resolved", "Q21"),
        "Triangle": ("ambiguous", None),
        "Ozark": ("resolved", "Q24"),
        "Nowhere": ("unresolved", None),
    }
    assert [t.detail for t in built.titles if t.key[0] == "Taken"] == ["no_tmdb_id"]
    rows = {r["qid"]: r for r in built.rows}
    assert set(rows) == {"Q11", "Q21", "Q24"}
    # a candidate outside the effective pilot is copied with its own role kept aside
    assert rows["Q11"]["role"] == "pairs" and rows["Q11"]["candidate_role"] == "reserve"
    # resolved rows have the candidate shape: bucket, series status, ids, source
    straw = rows["Q21"]
    assert (straw["role"], straw["bucket"], straw["bucket_basis"]) == (
        "pairs", "film:english", "only_language")
    assert straw["tmdb_id"] == 21 and straw["imdb_id"] == "tt21"
    assert straw["pair_title"] == "Straw Dogs" and straw["pair_ids"] == ["N2"]
    assert straw["year_match"] == "exact" and straw["candidate_role"] is None
    assert straw["source"] == {"kind": "wikidata_sparql", "license": "CC0-1.0",
                               "retrieved_at": NOW}
    assert straw["pairs_version"] == ps.PAIRS_VERSION and straw["selector_version"]
    ozark = rows["Q24"]
    assert (ozark["bucket"], ozark["series_status"], ozark["series_status_basis"]) == (
        "tv:english", "ended", "P582")
    assert built.qids == {("Finding Nemo", 2003, "movie"): "Q10",
                          ("Home Alone", 1990, "movie"): "Q11",
                          ("Straw Dogs", 1971, "movie"): "Q21",
                          ("Ozark", 2017, "tv_series"): "Q24"}

    resolved = ps.resolved_pairs(pairs, built.qids)
    assert [r["note"].split(":")[0] for r in resolved] == ["N2"]  # N1, N3, M1 have a gap
    assert resolved[0]["expectation"] == "should_not_match"

    scored = ps.scorability(pairs, built.qids, lambda q: None if q == "Q10" else "no plot file")
    assert [s["scorable"] for s in scored] == [False, False, False, False]
    assert scored[0]["problems"] == ["Taken (2008): no QID"]
    summary = ps.summary(built, scored)
    assert summary["title_status"] == {"in_pilot": 1, "ineligible": 1,
                                       "candidate_outside_pilot": 1, "resolved": 2,
                                       "ambiguous": 1, "unresolved": 1}
    text = ps.format_summary(summary)
    assert "AMBIGUOUS: Triangle (2009, movie) in N3" in text
    assert "INELIGIBLE: Taken" in text and "UNRESOLVED: Nowhere" in text


def test_build_routes_an_alias_hit_back_to_its_candidate() -> None:
    """A candidate whose label differs from the pairs file (found by the Wikidata alias) is
    still a candidate: no duplicate row."""
    se7en = {**item("Q190908", "Seven", 1995), "role": "pilot", "bucket_rank": 1,
             "bucket": "film:english"}
    items = {"Q190908": {**item("Q190908", "Seven", 1995), "seed_labels": ["Se7en"]}}
    pairs = [pair("M1", ("Se7en", 1995, "movie"), ("Seven", 1995, "movie"))]
    built = ps.build(pairs, [se7en], items, in_pilot=lambda q: True)
    assert built.rows == []
    assert [t.status for t in built.titles] == ["in_pilot", "in_pilot"]
    assert "same QID as Se7en" in (built.titles[1].detail or "")


def test_an_excluded_qid_is_reported_and_left_out() -> None:
    """QA S1: Q4384067's enwiki article is the Code Geass compilation films, not the series."""
    assert "Q4384067" in ps.EXCLUDED and "Q207981" in ps.EXCLUDED["Q4384067"]
    geass = item("Q4384067", "Code Geass Lelouch of the Rebellion", 2006, "tv_series",
                 seed_labels=["Code Geass"])
    pairs = [pair("M19", ("Death Note", 2006, "tv_series"), ("Code Geass", 2006, "tv_series"))]
    for cands, items in (([], {"Q4384067": geass}), ([{**geass, "role": "reserve"}], {})):
        built = ps.build(pairs, cands, items, in_pilot=lambda q: False)
        t = [t for t in built.titles if t.key[0] == "Code Geass"][0]
        assert (t.status, t.qid) == ("excluded", "Q4384067")
        assert all(r["qid"] != "Q4384067" for r in built.rows)
        assert ("Code Geass", 2006, "tv_series") not in built.qids
        scored = ps.scorability(pairs, built.qids, lambda q: None, built.excluded)
        assert scored[0]["scorable"] is False
        assert "Code Geass (2006): Q4384067 excluded" in scored[0]["problems"]
        assert "EXCLUDED: Code Geass" in ps.format_summary(ps.summary(built, scored))
        assert ps.resolved_pairs(pairs, built.qids) == []


def test_resolved_csv_loads_in_the_evaluator(tmp_path: Path) -> None:
    rows = [{"title_a": "Q1", "title_b": "Q2", "expectation": "should_match",
             "note": "M1: A (2000) / B, the sequel (2001)"}]
    path = tmp_path / "resolved.csv"
    path.write_text(ps.resolved_csv(rows), encoding="utf-8")
    loaded = load_resolved(path)
    assert [(p.title_a, p.title_b, p.expectation) for p in loaded] == [
        ("Q1", "Q2", "should_match")]
    assert loaded[0].note == "M1: A (2000) / B, the sequel (2001)"


def test_gold_resolve_pairs_sees_pair_titles() -> None:
    pairs = [pair("M1", ("How the Grinch Stole Christmas", 2000, "movie"),
                  ("Bad Santa", 2003, "movie"))]
    rows = [{"qid": "Q1", "title": "Dr. Seuss' How the Grinch Stole Christmas", "year": 2000,
             "media_type": "movie", "pair_title": "How the Grinch Stole Christmas"},
            {"qid": "Q2", "title": "Bad Santa", "year": 2003, "media_type": "movie"}]
    assert resolve_pairs(pairs, rows)[0]["qid_a"] == "Q1"


# ---------- CLI ----------


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"real network access attempted: {args[:1]}")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def _binding(qid: str, label: str) -> dict[str, Any]:
    return {"item": {"type": "uri", "value": f"http://www.wikidata.org/entity/{qid}"},
            "sitelinks": {"type": "literal", "value": "10"},
            "seed_label": {"xml:lang": "en", "type": "literal", "value": label}}


def _fake() -> FakeWikimedia:
    """The recorded fixtures, with the label lookup answering for the pair titles."""
    fake = FakeWikimedia()
    fake.data["sparql"]["seed"] = {
        "head": {"vars": ["item", "sitelinks", "seed_label"]},
        "results": {"bindings": [_binding("Q9000003", "Harbor Lights"),
                                 _binding("Q9000006", "Voices of the Valley"),
                                 _binding("Q9000001", "Lantern Keeper")]}}
    return fake


PAIRS_CSV = (
    "pair_id,expect,title_a,year_a,type_a,title_b,year_b,type_b,shared_shape,why,status\n"
    "M01,match,The Lantern Keeper,1994,movie,Harbor Lights,2013,tv_series,s,w,proposed\n"
    "N01,no_match,The Lantern Keeper,1994,movie,Voices of the Valley,1999,movie,s,w,proposed\n"
    "N02,no_match,Harbor Lights,2013,tv_series,Nowhere Film,2000,movie,s,w,proposed\n"
)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Candidates from the fixtures, cut down to the Lantern Keeper (no effective pilot yet)."""
    d = tmp_path / "data"
    (d / "config").mkdir(parents=True)
    (d / "config" / "gold_seed_titles.csv").write_text(
        "title,year,media_type,region,guessed_plot,guessed_arc,note\n"
        "Lantern Keeper,1994,movie,english,rebirth,man_in_a_hole,\n", encoding="utf-8")
    (d / "config" / "similarity_pairs.csv").write_text(PAIRS_CSV, encoding="utf-8")
    assert run(d, "candidates", fake=FakeWikimedia())[0] == 0
    path = d / "pilot_candidates.jsonl"
    rows = [r for r in read_jsonl(path) if r["qid"] == "Q9000001"]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return d


def run(data_dir: Path, *argv: str, fake: FakeWikimedia | None = None) -> tuple[int, str]:
    out: list[str] = []
    code = main(["--data-dir", str(data_dir), *argv], transport=fake, clock=lambda: NOW,
                sleep=lambda s: None, log=out.append)
    return code, "\n".join(out)


def test_pairs_then_plots_pairs_end_to_end(data_dir: Path) -> None:
    candidates_before = (data_dir / "pilot_candidates.jsonl").read_bytes()
    code, out = run(data_dir, "pairs", fake=_fake())
    assert code == 0, out
    rows = {r["qid"]: r for r in read_jsonl(data_dir / "pairs_candidates.jsonl")}
    assert set(rows) == {"Q9000001", "Q9000003"}
    assert rows["Q9000001"]["candidate_role"] == "pilot"  # not in an effective pilot
    harbor = rows["Q9000003"]
    assert (harbor["role"], harbor["bucket"], harbor["tmdb_id"], harbor["series_status"]) == (
        "pairs", "tv:english", 90003, "ended")
    assert "INELIGIBLE: Voices of the Valley" in out and "genre:documentary" in out
    assert "UNRESOLVED: Nowhere Film" in out
    resolved = load_resolved(data_dir / "similarity_pairs_resolved.csv")
    assert [(p.title_a, p.title_b, p.expectation) for p in resolved] == [
        ("Q9000001", "Q9000003", "should_match")]
    summary = json.loads((data_dir / "reports" / "pairs_summary.json").read_text())
    assert summary["pairs_scorable"] == 0 and summary["pairs_resolved"] == 1

    fake = FakeWikimedia()
    code, out = run(data_dir, "plots", "--pairs", fake=fake)
    assert code == 0, out
    # QA S2: a pilot-role title's plot file belongs to the pilot run; --pairs leaves it alone
    assert "not fetched for the pairs: Q9000001" in out
    assert not (data_dir / "plots" / "Q9000001.json").exists()
    harbor_file = json.loads((data_dir / "plots" / "Q9000003.json").read_text())
    assert harbor_file["status"] == "ok" and harbor_file["fetched_for"] == "pairs"
    summary = json.loads((data_dir / "reports" / "pairs_summary.json").read_text())
    assert summary["pairs_set_plots"]["fetched"] == 1
    scorable = {p["pair_id"]: p["scorable"] for p in summary["pair_status"]}
    assert summary["pairs_set_plots"]["annotatable"] == 1 and scorable["M01"] is False
    assert not scorable["N01"] and not scorable["N02"]
    # the report re-resolves from the HTTP cache: the lookups are not repeated on the network
    assert not any(r.host == "query.wikidata.org" and b"gold-seed" in (r.data or b"")
                   for r in fake.requests)
    assert "UNRESOLVED: Nowhere Film" in out  # still reported after the fetch

    # the pairs commands don't write pilot files (byte-level check with real files:
    # test_pairs_commands_leave_pilot_and_gold_files_untouched)
    assert (data_dir / "pilot_candidates.jsonl").read_bytes() == candidates_before
    assert not (data_dir / "pilot_effective.jsonl").exists()
    assert not (data_dir / "reports" / "plots_summary.json").exists()

    again = FakeWikimedia()
    assert run(data_dir, "plots", "--pairs", fake=again)[0] == 0
    assert again.requests == []  # resumable: nothing fetched twice

    # the pilot run fetches its own title; the pairs report then sees it
    assert run(data_dir, "plots", "--qid", "Q9000001", fake=FakeWikimedia())[0] == 0
    pilot_file = json.loads((data_dir / "plots" / "Q9000001.json").read_text())
    assert pilot_file["status"] == "ok" and "fetched_for" not in pilot_file
    code, out = run(data_dir, "plots", "--pairs", "--refresh", fake=FakeWikimedia())
    assert code == 0 and "not fetched for the pairs: Q9000001" in out
    assert json.loads((data_dir / "plots" / "Q9000001.json").read_text()) == pilot_file
    summary = json.loads((data_dir / "reports" / "pairs_summary.json").read_text())
    assert {p["pair_id"]: p["scorable"] for p in summary["pair_status"]}["M01"] is True


def test_pairs_commands_leave_pilot_and_gold_files_untouched(data_dir: Path) -> None:
    """QA S3: with an effective pilot, a pilot report, gold files and a pilot-role title whose
    plot was skipped (it joins the pairs set: Ted Lasso, Death Note, Shrinking), neither
    ``pairs`` nor ``plots --pairs`` (even with --refresh) changes a byte of them."""
    skipped = {"fetcher_version": "1.5.9", "qid": "Q9000001", "status": "skipped",
               "skip_reason": "too_short", "skip_detail": "premise has 84 words"}
    write_json_atomic(data_dir / "plots" / "Q9000001.json", skipped)
    (data_dir / "pilot_effective.jsonl").write_text(
        '{"qid": "Q9000099", "title": "Other", "bucket": "film:english", "role": "pilot"}\n',
        encoding="utf-8")
    (data_dir / "reports").mkdir(exist_ok=True)
    (data_dir / "reports" / "plots_summary.json").write_text('{"fetched": 560}\n',
                                                             encoding="utf-8")
    (data_dir / "gold" / "texts").mkdir(parents=True)
    (data_dir / "gold" / "gold_selection.jsonl").write_text('{"qid": "Q9000001"}\n',
                                                           encoding="utf-8")
    (data_dir / "gold" / "texts" / "Q9000001.txt").write_text("gold text\n", encoding="utf-8")
    guarded = ["pilot_candidates.jsonl", "pilot_effective.jsonl", "reports/plots_summary.json",
               "gold/gold_selection.jsonl", "gold/texts/Q9000001.txt", "plots/Q9000001.json"]
    before = {p: (data_dir / p).read_bytes() for p in guarded}

    code, out = run(data_dir, "pairs", fake=_fake())
    assert code == 0, out
    rows = {r["qid"]: r for r in read_jsonl(data_dir / "pairs_candidates.jsonl")}
    # the skipped pilot title is in the pairs set, with its pilot role kept aside
    assert rows["Q9000001"]["role"] == "pairs" and rows["Q9000001"]["candidate_role"] == "pilot"
    for argv in (("plots", "--pairs"), ("plots", "--pairs", "--refresh"),
                 ("plots", "--pairs", "--refresh", "--qid", "Q9000001")):
        code, out = run(data_dir, *argv, fake=FakeWikimedia())
        assert code == 0, out
        assert "not fetched for the pairs: Q9000001" in out
    assert {p: (data_dir / p).read_bytes() for p in guarded} == before
    summary = json.loads((data_dir / "reports" / "pairs_summary.json").read_text())
    m01 = [p for p in summary["pair_status"] if p["pair_id"] == "M01"][0]
    assert m01["scorable"] is False
    assert any("Q9000001): skipped: too_short" in p for p in m01["problems"])
    assert json.loads((data_dir / "plots" / "Q9000003.json").read_text())["fetched_for"] == (
        "pairs")


def test_pairs_files_never_move_the_pilot(tmp_path: Path) -> None:
    """QA S2: a reserve fetched for a pair (Home Alone, rank 1 in film:english) is invisible
    to the pilot: the report doesn't count it, it fills no slot, and backfill fetches it afresh
    only when it reaches it in rank order."""
    paths = DataPaths.resolve(str(tmp_path))
    rows = [{"qid": q, "title": f"Film {q}", "year": 2019, "media_type": "movie",
             "region": "english", "language": "english", "decade": "2010s",
             "bucket": "film:english", "role": role, "bucket_rank": rank}
            for q, role, rank in (("Q11", "pilot", 1), ("Q12", "pilot", 2),
                                  ("Q21", "reserve", 1), ("Q22", "reserve", 2))]
    write_json_atomic(paths.plot_file("Q11"), ok_plot("Q11"))
    write_json_atomic(paths.plot_file("Q12"), {"qid": "Q12", "status": "skipped",
                                               "skip_reason": "too_short",
                                               "fetcher_version": "1.5.9"})
    write_json_atomic(paths.plot_file("Q22"), {**ok_plot("Q22"), "fetched_for": "pairs"})
    assert not_annotatable(paths, "Q22") is None  # the pairs can use it
    assert existing_status(paths, "Q22") is None  # the pilot can't see it
    assert existing_status(paths, "Q22", include_pairs=True) is not None
    assert [r["qid"] for r in effective_pilot(paths, rows)] == ["Q11"]
    assert plots_report(paths, rows)["fetched"] == 2
    assert not pairs_owned(paths, "Q11") and pairs_owned(paths, "Q22")
    assert pairs_owned(paths, "Q99")  # no file yet

    class Fetcher:
        def __init__(self, skip: set[str]) -> None:
            self.skip, self.fetched = skip, []

        def fetch(self, c: dict[str, Any]) -> dict[str, Any]:
            self.fetched.append(c["qid"])
            if c["qid"] in self.skip:
                return {"qid": c["qid"], "status": "skipped", "skip_reason": "too_short",
                        "fetcher_version": "1.5.9"}
            return ok_plot(c["qid"])

    # backfill takes the better-ranked reserve first; the pairs file is left as it is
    f = Fetcher(set())
    run_plots(paths, rows, f, backfill=True, log=lambda m: None)
    assert f.fetched == ["Q21"]
    assert [r["qid"] for r in effective_pilot(paths, rows)] == ["Q11", "Q21"]
    assert existing_status(paths, "Q22", include_pairs=True)["fetched_for"] == "pairs"
    # when backfill does reach it, it is fetched afresh for the pilot (no marker)
    write_json_atomic(paths.plot_file("Q21"), {"qid": "Q21", "status": "skipped",
                                               "skip_reason": "too_short",
                                               "fetcher_version": "1.5.9"})
    f = Fetcher(set())
    run_plots(paths, rows, f, backfill=True, log=lambda m: None)
    assert f.fetched == ["Q22"]
    assert "fetched_for" not in existing_status(paths, "Q22")
    assert [r["qid"] for r in effective_pilot(paths, rows)] == ["Q11", "Q22"]


def ok_plot(qid: str) -> dict[str, Any]:
    return {**ingest_plot(qid, tmdb_id=int(qid[1:])), "fetcher_version": "1.5.9",
            "word_count": 200, "section": {"heading": "plot"},
            "permalink": f"https://en.wikipedia.org/w/index.php?title={qid}&oldid=1"}


def test_pairs_dry_run_and_guards(data_dir: Path) -> None:
    fake = _fake()
    code, out = run(data_dir, "pairs", "--dry-run", fake=fake)
    assert code == 0 and fake.requests == [] and "not cached yet" in out
    assert not (data_dir / "pairs_candidates.jsonl").exists()
    code, out = run(data_dir, "plots", "--pairs", fake=FakeWikimedia())
    assert code == 2 and "run the 'pairs' command first" in out
    run(data_dir, "pairs", fake=_fake())
    code, out = run(data_dir, "plots", "--pairs", "--backfill", fake=FakeWikimedia())
    assert code == 2 and "--backfill" in out
    fake = FakeWikimedia()
    code, out = run(data_dir, "plots", "--pairs", "--dry-run", fake=fake)
    assert code == 0 and fake.requests == [] and "would fetch Q9000003" in out
    assert not (data_dir / "plots").exists() or not any((data_dir / "plots").iterdir())
    fake = FakeWikimedia()
    run(data_dir, "plots", "--pairs", "--qid", "Q9000003", fake=fake)
    assert sorted(p.name for p in (data_dir / "plots").glob("Q*.json")) == ["Q9000003.json"]


def test_pairs_refuses_an_invalid_pairs_file(data_dir: Path) -> None:
    (data_dir / "config" / "similarity_pairs.csv").write_text(
        PAIRS_CSV.replace("M01,match", "M01,maybe"), encoding="utf-8")
    code, out = run(data_dir, "pairs", fake=_fake())
    assert code == 2 and "expect must be one of" in out
    assert not (data_dir / "pairs_candidates.jsonl").exists()
