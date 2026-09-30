"""Compare model annotations with gold labels (docs/NARRATIVE_SCHEMA.md section 14).

Headline (Phase 1 exit metric, decided 2026-09-29): exact match of
``archetypal_plot.primary.label`` against gold on at least 80% of gold titles. The denominator
is every gold title with ``outcome: annotated``; a model abstention, or a title the model never
annotated, counts as a miss. Gold titles the labeler abstained on have no primary and are
reported separately.

Also reported (not gating): kappa and lenient agreement on the primary plot; blueprint;
emotional arc on unflagged titles with fallback and reduced-shape titles broken out; per-term
precision/recall/F1 for plots, stages and tags; surface fields; calibration by confidence band;
abstention; flat and reduced arc rates split by movies and series; spoiler leaks (from human
flags, when provided); cost per title; and the pair check (stub).

Every model record in one report must share (schema_version, prompt_version, model_version).
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from laminary_pipeline.annotate.client import Usage
from laminary_pipeline.annotate.cost import price_usage
from laminary_pipeline.annotate.inputs import title_key
from laminary_pipeline.annotation import validate_record
from laminary_pipeline.arc import derive_arc
from laminary_pipeline.evaluate import metrics as m
from laminary_pipeline.evaluate.pairs import PairReport
from laminary_pipeline.evaluate.pairs import render_markdown as render_pairs

EXIT_TARGET = 0.80
ABSTAINED = "(abstained)"
MISSING = "(no model record)"


class EvaluationInputError(ValueError):
    pass


# --- loading -------------------------------------------------------------------------------


def load_records(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Records from .json (object or list) and .jsonl files, or directories of them. Every
    record must pass ``validate_record``; one invalid record stops the evaluation."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files += sorted(p for p in path.rglob("*") if p.suffix in (".json", ".jsonl"))
        else:
            files.append(path)
    records: list[dict[str, Any]] = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        if f.suffix == ".jsonl":
            items = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            data = json.loads(text)
            items = data if isinstance(data, list) else [data]
        for i, rec in enumerate(items):
            problems = validate_record(rec)
            if problems:
                raise EvaluationInputError(f"{f} record {i}: {'; '.join(problems[:5])}")
            records.append(rec)
    return records


def index_gold(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    gold: dict[str, dict[str, Any]] = {}
    for rec in records:
        if rec["record_kind"] != "gold_label":
            raise EvaluationInputError(f"not a gold_label record: {title_key(rec['title'])}")
        key = title_key(rec["title"])
        if key in gold:
            raise EvaluationInputError(f"two gold records for {key}")
        gold[key] = rec
    return gold


@dataclass(frozen=True)
class Group:
    schema_version: str
    prompt_version: str
    model_version: str


def group_of(rec: dict[str, Any]) -> Group:
    ann = rec["provenance"]["annotator"]
    return Group(rec["schema_version"], ann["prompt_version"], ann["model_version"])


def index_model(records: list[dict[str, Any]]) -> tuple[Group, dict[str, dict[str, Any]]]:
    groups = {group_of(r) for r in records if r["record_kind"] == "llm_annotation"}
    if len(groups) != 1:
        raise EvaluationInputError(
            "model records must share one (schema, prompt, model) group; "
            f"found {sorted(groups, key=str)}"
        )
    out: dict[str, dict[str, Any]] = {}
    for rec in records:
        if rec["record_kind"] != "llm_annotation":
            raise EvaluationInputError("non-LLM record among model annotations")
        key = title_key(rec["title"])
        if key in out:
            raise EvaluationInputError(f"two model records for {key}; evaluate one run at a time")
        out[key] = rec
    return groups.pop(), out


# --- helpers -------------------------------------------------------------------------------


def _annotated(rec: dict[str, Any] | None) -> bool:
    return rec is not None and rec["outcome"] == "annotated"


def _primary(rec: dict[str, Any]) -> str:
    return rec["layers"]["archetypal_plot"]["primary"]["label"]


def _arc(rec: dict[str, Any]) -> dict[str, Any]:
    return rec["layers"]["structural_skeleton"]["emotional_arc"]


def _arc_flag(arc: dict[str, Any]) -> str | None:
    if arc.get("net_change_fallback"):
        return "fallback"
    if arc.get("reduced_shape"):
        return "reduced"
    return None


def _source_hashes(rec: dict[str, Any]) -> list[str]:
    return sorted(s["content_sha256"] for s in rec["provenance"]["sources"])


def _acc(correct: int, total: int) -> dict[str, Any]:
    return {"correct": correct, "total": total, "accuracy": m.ratio(correct, total)}


# --- sections ------------------------------------------------------------------------------


def _headline(gold: dict, model: dict) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows, correct, abstained, missing = [], 0, 0, 0
    for key, g in sorted(gold.items()):
        if not _annotated(g):
            continue
        rec = model.get(key)
        if rec is None:
            got, missing = MISSING, missing + 1
        elif rec["outcome"] == "abstained":
            got, abstained = ABSTAINED, abstained + 1
        else:
            got = _primary(rec)
        hit = got == _primary(g)
        correct += hit
        rows.append(
            {
                "title_key": key,
                "name": g["title"]["name"],
                "gold": _primary(g),
                "model": got,
                "correct": hit,
            }
        )
    total = len(rows)
    acc = m.ratio(correct, total)
    return {
        "metric": "archetypal_plot.primary.label exact match vs gold",
        "target": EXIT_TARGET,
        **_acc(correct, total),
        "passes": acc is not None and acc >= EXIT_TARGET,
        "model_abstained_counted_as_miss": abstained,
        "no_model_record_counted_as_miss": missing,
    }, rows


def _primary_extras(gold: dict, model: dict, rows: list[dict[str, Any]]) -> dict[str, Any]:
    kappa = m.cohen_kappa([r["gold"] for r in rows], [r["model"] for r in rows])
    lenient = sum(
        1
        for r in rows
        if r["model"] in gold[r["title_key"]]["layers"]["archetypal_plot"]["plots"]
        and gold[r["title_key"]]["layers"]["archetypal_plot"]["plots"][r["model"]]["present"]
    )
    confusion: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        confusion[r["gold"]][r["model"]] += 1
    return {
        "kappa": kappa,
        "kappa_note": "abstentions and missing records are their own category",
        "lenient": {
            **_acc(lenient, len(rows)),
            "rule": "model primary judged present in gold plots",
        },
        "confusion": {g: dict(c) for g, c in sorted(confusion.items())},
    }


def _both_annotated(gold: dict, model: dict) -> list[tuple[str, dict, dict]]:
    return [
        (k, g, model[k])
        for k, g in sorted(gold.items())
        if _annotated(g) and _annotated(model.get(k))
    ]


def _blueprint(pairs: list) -> dict[str, Any]:
    hits = sum(
        g["layers"]["mythic_blueprint"]["blueprint"]["label"]
        == r["layers"]["mythic_blueprint"]["blueprint"]["label"]
        for _, g, r in pairs
    )
    return _acc(hits, len(pairs))


def _emotional_arc(pairs: list, gold: dict, model: dict) -> dict[str, Any]:
    buckets: dict[str, list[bool]] = {"unflagged": [], "fallback": [], "reduced": []}
    gold_points, label_agree, mad = 0, [], []
    for _, g, r in pairs:
        ga, ra = _arc(g), _arc(r)
        flags = {_arc_flag(ga), _arc_flag(ra)} - {None}
        bucket = "fallback" if "fallback" in flags else "reduced" if flags else "unflagged"
        buckets[bucket].append(ga["label"] == ra["label"])
        gp = g["layers"]["structural_skeleton"].get("arc_points")
        if gp:
            gold_points += 1
            rp = r["layers"]["structural_skeleton"]["arc_points"]
            label_agree.append(derive_arc(gp).label == derive_arc(rp).label)
            mad.append(sum(abs(a - b) for a, b in zip(gp, rp, strict=True)) / len(gp))
    not_scored = sum(1 for k, g in gold.items() if _annotated(g) and not _annotated(model.get(k)))
    return {
        "headline_unflagged": _acc(sum(buckets["unflagged"]), len(buckets["unflagged"])),
        "fallback": {
            "count": len(buckets["fallback"]),
            **_acc(sum(buckets["fallback"]), len(buckets["fallback"])),
        },
        "reduced_shape": {
            "count": len(buckets["reduced"]),
            **_acc(sum(buckets["reduced"]), len(buckets["reduced"])),
        },
        "not_scored_model_abstained_or_missing": not_scored,
        "gold_points": {
            "titles": gold_points,
            "derived_label_agreement": m.ratio(sum(label_agree), len(label_agree)),
            "mean_abs_point_difference": m.mean(mad),
        },
    }


PRESENCE = {
    "plots": ("layers", "archetypal_plot", "plots"),
    "stages": ("layers", "mythic_blueprint", "stages"),
    "tags": ("beat_tags", "tags"),
}


def _get(rec: dict[str, Any], path: tuple[str, ...]) -> Any:
    for p in path:
        rec = rec[p]
    return rec


def _presence(pairs: list) -> tuple[dict[str, Any], list[tuple[float, bool]]]:
    out: dict[str, Any] = {}
    calib: list[tuple[float, bool]] = []
    for name, path in PRESENCE.items():
        per_term: dict[str, m.Confusion] = defaultdict(m.Confusion)
        for _, g, r in pairs:
            gj, rj = _get(g, path), _get(r, path)
            for term, judgment in rj.items():
                if term not in gj:
                    continue  # not assessed on the gold side (older schema)
                per_term[term].add(gj[term]["present"], judgment["present"])
                calib.append((judgment["confidence"], gj[term]["present"] == judgment["present"]))
        out[name] = {t: c.summary() for t, c in sorted(per_term.items())}
    return out, calib


def _surface(pairs: list) -> dict[str, Any]:
    fields = {
        "setting_period": ("layers", "surface_story", "setting_period"),
        "protagonist_structure": ("layers", "surface_story", "protagonist_structure"),
        "chronology": ("layers", "structural_skeleton", "chronology"),
    }
    out: dict[str, Any] = {}
    for name, path in fields.items():
        hits = total = 0
        for _, g, r in pairs:
            try:
                gv = _get(g, path)
            except KeyError:
                continue  # optional on gold records
            total += 1
            hits += gv == _get(r, path)
        out[name] = _acc(hits, total)
    overlaps = []
    for _, g, r in pairs:
        gs = g["layers"].get("surface_story")
        if gs:
            overlaps.append(m.jaccard(gs["tones"], r["layers"]["surface_story"]["tones"]))
    out["tones_mean_jaccard"] = {"titles": len(overlaps), "mean": m.mean(overlaps)}
    return out


def _calibration(pairs: list, presence_calib: list) -> dict[str, Any]:
    primary = [
        (r["layers"]["archetypal_plot"]["primary"]["confidence"], _primary(g) == _primary(r))
        for _, g, r in pairs
    ]
    blueprint = [
        (
            r["layers"]["mythic_blueprint"]["blueprint"]["confidence"],
            g["layers"]["mythic_blueprint"]["blueprint"]["label"]
            == r["layers"]["mythic_blueprint"]["blueprint"]["label"],
        )
        for _, g, r in pairs
    ]
    arc = [
        (_arc(r)["confidence"], _arc(g)["label"] == _arc(r)["label"])
        for _, g, r in pairs
        if _arc_flag(_arc(g)) is None and _arc_flag(_arc(r)) is None
    ]
    return {
        "primary": m.calibration(primary),
        "blueprint": m.calibration(blueprint),
        "emotional_arc_unflagged": m.calibration(arc),
        "presence_judgments": m.calibration(presence_calib),
    }


def _abstention(gold: dict, model: dict) -> dict[str, Any]:
    matched = [k for k in gold if k in model]
    abstained = [k for k in matched if model[k]["outcome"] == "abstained"]
    on_labelable = [k for k in abstained if _annotated(gold[k])]
    gold_abstained = [k for k in gold if gold[k]["outcome"] == "abstained"]
    return {
        "rate": m.ratio(len(abstained), len(matched)),
        "model_abstentions": len(abstained),
        "on_titles_gold_could_label": sorted(on_labelable),
        "gold_abstained_titles": len(gold_abstained),
        "model_also_abstained_on_those": sum(
            1 for k in gold_abstained if k in model and model[k]["outcome"] == "abstained"
        ),
    }


def flat_reduced_rates(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, list[str | None]] = defaultdict(list)
    for rec in records:
        if _annotated(rec):
            by_type[rec["title"]["media_type"]].append(_arc_flag(_arc(rec)))
    return {
        media: {
            "titles": len(flags),
            "net_change_fallback_rate": m.ratio(flags.count("fallback"), len(flags)),
            "reduced_shape_rate": m.ratio(flags.count("reduced"), len(flags)),
        }
        for media, flags in sorted(by_type.items())
    }


def spoiler_leaks(flags: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Human flags on safe_text: {"title_key", "field", "grade": "none"|"mild"|"major"}.
    One line per reviewed field; a title counts as reviewed if it has any line."""
    if flags is None:
        return {"status": "not measured: needs gold labelers' safe_text flags", "target_major": 0}
    reviewed = {f["title_key"] for f in flags}
    major = sorted({f["title_key"] for f in flags if f["grade"] == "major"})
    mild = {f["title_key"] for f in flags if f["grade"] == "mild"}
    return {
        "status": "measured",
        "titles_reviewed": len(reviewed),
        "major_leak_titles": major,
        "major_leaks": len(major),
        "target_major": 0,
        "passes_major_target": not major,
        "mild_rate": m.ratio(len(mild), len(reviewed)),
    }


def cost_section(cost_reports: list[dict[str, Any]], records: list[dict[str, Any]]) -> dict:
    if cost_reports:
        usd = sum(c["usd_total"] for c in cost_reports)
        titles = sum(c["titles_attempted"] for c in cost_reports)
        stored = sum(c["records_stored"] for c in cost_reports)
        return {
            "source": "run cost.json (all attempts, failures included; unconfirmed prices)",
            "usd_total": round(usd, 5),
            "usd_per_title_attempted": round(usd / titles, 5) if titles else None,
            "usd_per_record_stored": round(usd / stored, 5) if stored else None,
        }
    usd = 0.0
    for rec in records:
        u = rec["provenance"]["usage"]
        usage = Usage(u["input_tokens"], u["output_tokens"], u.get("cache_read_input_tokens", 0))
        usd += price_usage(rec["provenance"]["annotator"]["model_version"], usage, batch=u["batch"])
    return {
        "source": "record usage (excludes cache writes: the schema has no field for them; "
        "unconfirmed prices)",
        "usd_total": round(usd, 5),
        "usd_per_record_stored": round(usd / len(records), 5) if records else None,
    }


# --- report --------------------------------------------------------------------------------


def evaluate(
    gold_records: list[dict[str, Any]],
    model_records: list[dict[str, Any]],
    *,
    run_records: list[dict[str, Any]] | None = None,
    cost_reports: list[dict[str, Any]] | None = None,
    leak_flags: list[dict[str, Any]] | None = None,
    pair_report: PairReport | None = None,
) -> dict[str, Any]:
    gold = index_gold(gold_records)
    group, all_model = index_model(model_records)
    model = {k: v for k, v in all_model.items() if k in gold}
    mismatched = sorted(k for k in model if _source_hashes(model[k]) != _source_hashes(gold[k]))
    headline, rows = _headline(gold, model)
    pairs = _both_annotated(gold, model)
    presence, presence_calib = _presence(pairs)
    return {
        "group": {
            "schema_version": group.schema_version,
            "prompt_version": group.prompt_version,
            "model_version": group.model_version,
            "run_ids": sorted({r["provenance"]["annotator"]["run_id"] for r in model_records}),
        },
        "counts": {
            "gold_titles": len(gold),
            "gold_annotated": sum(_annotated(g) for g in gold.values()),
            "gold_abstained": sum(not _annotated(g) for g in gold.values()),
            "model_records_on_gold_titles": len(model),
            "both_annotated": len(pairs),
            "source_hash_mismatches": mismatched,
        },
        "headline": headline,
        "primary_plot": _primary_extras(gold, model, rows),
        "blueprint": _blueprint(pairs),
        "emotional_arc": _emotional_arc(pairs, gold, model),
        "presence": presence,
        "surface": _surface(pairs),
        "calibration": _calibration(pairs, presence_calib),
        "abstention": _abstention(gold, model),
        "flat_and_reduced_arcs": {
            "gold_set_model": flat_reduced_rates(model.values()),
            "gold_set_gold": flat_reduced_rates(gold.values()),
            "whole_run": flat_reduced_rates(
                run_records if run_records is not None else all_model.values()
            ),
        },
        "spoiler_leaks": spoiler_leaks(leak_flags),
        "cost": cost_section(cost_reports or [], list(all_model.values())),
        "pairs": (pair_report.as_dict() if pair_report else {"status": "no pairs file given"}),
        "per_title": rows,
    }


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _num(x: float | None, digits: int = 3) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}"


def render_markdown(report: dict[str, Any], pair_report: PairReport | None = None) -> str:
    g, c, h = report["group"], report["counts"], report["headline"]
    arc = report["emotional_arc"]
    lines = [
        "# Narrative annotation evaluation",
        "",
        f"Schema {g['schema_version']} · prompt {g['prompt_version']} · model "
        f"{g['model_version']} · runs {', '.join(g['run_ids'])}",
        "",
        "## Phase 1 exit metric",
        "",
        f"**Primary Booker plot exact match: {h['correct']}/{h['total']} = "
        f"{_pct(h['accuracy'])}** (target {_pct(h['target'])}): "
        f"**{'PASS' if h['passes'] else 'NOT MET'}**.",
        f"Misses include {h['model_abstained_counted_as_miss']} model abstentions and "
        f"{h['no_model_record_counted_as_miss']} titles with no model record.",
        "",
        f"Gold titles: {c['gold_titles']} ({c['gold_annotated']} annotated, "
        f"{c['gold_abstained']} abstained by the labeler). Both annotated: {c['both_annotated']}.",
    ]
    if c["source_hash_mismatches"]:
        lines.append(
            f"**Warning:** {len(c['source_hash_mismatches'])} titles were labeled from a "
            f"different summary than the model saw: {', '.join(c['source_hash_mismatches'])}."
        )
    pp = report["primary_plot"]
    lines += [
        "",
        "## Also reported (not gating)",
        "",
        "| Measure | Result |",
        "|---|---|",
        f"| Primary plot kappa | {_num(pp['kappa'])} |",
        f"| Primary plot lenient (model primary present in gold plots) | "
        f"{pp['lenient']['correct']}/{pp['lenient']['total']} = "
        f"{_pct(pp['lenient']['accuracy'])} |",
        f"| Blueprint exact match | {report['blueprint']['correct']}/"
        f"{report['blueprint']['total']} = {_pct(report['blueprint']['accuracy'])} |",
        f"| Emotional arc, unflagged titles | {arc['headline_unflagged']['correct']}/"
        f"{arc['headline_unflagged']['total']} = {_pct(arc['headline_unflagged']['accuracy'])} |",
        f"| Emotional arc, fallback titles | {arc['fallback']['count']} titles, agreement "
        f"{_pct(arc['fallback']['accuracy'])} |",
        f"| Emotional arc, reduced-shape titles | {arc['reduced_shape']['count']} titles, "
        f"agreement {_pct(arc['reduced_shape']['accuracy'])} |",
        f"| Arc not scored (model abstained or missing) | "
        f"{arc['not_scored_model_abstained_or_missing']} |",
        f"| Gold arc points: derived-label agreement / mean abs difference | "
        f"{_pct(arc['gold_points']['derived_label_agreement'])} / "
        f"{_num(arc['gold_points']['mean_abs_point_difference'])} "
        f"({arc['gold_points']['titles']} titles) |",
        f"| Abstention rate | {_pct(report['abstention']['rate'])} "
        f"({report['abstention']['model_abstentions']} titles; "
        f"{len(report['abstention']['on_titles_gold_could_label'])} on titles gold labeled) |",
    ]
    sp = report["spoiler_leaks"]
    if sp["status"] == "measured":
        lines.append(
            f"| Spoiler leaks: major (target 0) / mild rate | {sp['major_leaks']} / "
            f"{_pct(sp['mild_rate'])} ({sp['titles_reviewed']} titles reviewed) |"
        )
    else:
        lines.append(f"| Spoiler leaks | {sp['status']} |")
    cost = report["cost"]
    per_title = cost.get("usd_per_title_attempted") or cost.get("usd_per_record_stored")
    lines.append(
        f"| Cost | ${cost['usd_total']:.4f} total; ${_num(per_title, 5)} per title "
        f"({cost['source']}) |"
    )
    lines += ["", "## Presence judgments (per term)", ""]
    for name, terms in report["presence"].items():
        lines += [
            f"### {name}",
            "",
            "| Term | P | R | F1 | Accuracy | TP/FP/FN/TN |",
            "|---|---:|---:|---:|---:|---|",
        ]
        for term, s in terms.items():
            lines.append(
                f"| {term} | {_num(s['precision'], 2)} | {_num(s['recall'], 2)} | "
                f"{_num(s['f1'], 2)} | {_pct(s['accuracy'])} | "
                f"{s['tp']}/{s['fp']}/{s['fn']}/{s['tn']} |"
            )
        lines.append("")
    lines += [
        "## Calibration (accuracy by confidence band)",
        "",
        "| Band | Primary | Blueprint | Arc (unflagged) | Presence judgments |",
        "|---|---|---|---|---|",
    ]
    cal = report["calibration"]
    for band in cal["primary"]:
        cells = [
            f"{_pct(cal[k][band]['accuracy'])} (n={cal[k][band]['n']})"
            for k in ("primary", "blueprint", "emotional_arc_unflagged", "presence_judgments")
        ]
        lines.append(f"| {band} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "## Flat and reduced arcs",
        "",
        "| Set | Media | Titles | Fallback | Reduced |",
        "|---|---|---:|---:|---:|",
    ]
    for set_name, by_media in report["flat_and_reduced_arcs"].items():
        for media, s in by_media.items():
            lines.append(
                f"| {set_name} | {media} | {s['titles']} | "
                f"{_pct(s['net_change_fallback_rate'])} | "
                f"{_pct(s['reduced_shape_rate'])} |"
            )
    surf = report["surface"]
    lines += ["", "## Surface fields (informational)", ""]
    for name in ("setting_period", "protagonist_structure", "chronology"):
        lines.append(
            f"- {name}: {surf[name]['correct']}/{surf[name]['total']} = "
            f"{_pct(surf[name]['accuracy'])}"
        )
    lines.append(
        f"- tones mean Jaccard: {_num(surf['tones_mean_jaccard']['mean'])} "
        f"({surf['tones_mean_jaccard']['titles']} titles)"
    )
    lines += ["", "## Primary plot confusion (gold -> model)", ""]
    for gold_label, row in pp["confusion"].items():
        lines.append(f"- {gold_label}: " + ", ".join(f"{k} {v}" for k, v in sorted(row.items())))
    lines += [
        "",
        "## Per title",
        "",
        "| Title | Gold primary | Model primary | Match |",
        "|---|---|---|---|",
    ]
    for r in report["per_title"]:
        lines.append(
            f"| {r['name']} ({r['title_key']}) | {r['gold']} | {r['model']} | "
            f"{'yes' if r['correct'] else 'no'} |"
        )
    lines += [
        "",
        render_pairs(pair_report)
        if pair_report
        else "## Should / should NOT match pairs\n\nNo pairs file given.",
        "",
    ]
    return "\n".join(lines)
