"""Write the gold labeling sheet as CSV files (the fallback to ``gold_labels.xlsx``, which
``gold/workbook.py`` writes from the same rows) and the summary texts.

Three files (one per tab; File > Import > "Insert new sheet(s)" for each):

- ``gold_labels_template.csv`` -> tab "Labels": one row per title, a help row, then the titles.
  Prefilled columns carry the Wikipedia revision the labeler must read and the source fields the
  importer needs (so the sheet is self-contained).
- ``gold_labels_lists.csv`` -> tab "Lists": one column per dropdown. In "Labels", use Data >
  Data validation > "Dropdown (from a range)" pointing at these columns.
- ``gold_labels_readme.csv`` -> tab "README": short instructions and the column guide.
- ``texts/<QID>.txt``: the exact summary text the model receives for each title
  (``prompt.labeler_text``, UTF-8, no trailing newline). For a single source that is the plot
  file's ``text`` byte for byte, so its SHA-256 equals the sheet's ``source_sha256``. For a
  series summarized from season articles (DECISIONS 2026-10-01) it is every season's summary
  block exactly as the request carries it, marker naming the article included, in season
  order; the per-article hashes are in ``source_sha256``, separated by " | ", like the other
  ``source_*`` columns. Labelers read this file, not the Wikipedia page: the page also has
  tables, captions, hatnotes and notes that ingest strips (``ingest/text.py``), and a TV
  series' episode tables are dropped. The revision link stays in the sheet for attribution.
  These files hold CC BY-SA text and live under the gitignored data directory.
- ``summary_coverage`` (sheet column, guide 1.4.0, DECISIONS 2026-10-02): for a series summary
  that covers only some seasons, the exact line the annotate-1.2.0 request header carries
  ("Summary covers seasons 1–4 of 7."; ``prompt.labeler_coverage``), else empty. It is a column,
  not a header in the text file, so the text files stay byte-identical to the model's summary
  blocks.
- ``texts/manifest.csv``: one row per text file (filename, qid, title, sha256, word_count), so
  the ``texts`` folder can be uploaded to the Drive Laminary folder as is (DECISIONS
  2026-09-30) and checked after upload. Stale ``Q*.txt`` files from earlier runs are removed,
  so the folder holds exactly the files the manifest lists. Nothing is uploaded from here.

Titles the selector flagged ``double_label`` get two rows, ``label_slot`` 1 and 2, for two
different labelers working independently. The model's guessed plot/arc from the selector are
never written to the sheet.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from laminary_pipeline.annotate.config import DEFAULT_PROMPT_VERSION
from laminary_pipeline.annotate.inputs import (
    GatedInput,
    GateError,
    InputFormatError,
    NotAnnotatable,
    PlotInput,
    PlotSource,
    gate,
    ingest_sources,
    ingest_via,
    parse_coverage,
    parse_partial_season,
    parse_via_detail,
    parse_year_coverage,
)
from laminary_pipeline.annotate.prompt import (
    labeler_coverage,
    labeler_text,
    load_prompt,
    require_current_ingest,
)
from laminary_pipeline.gold.columns import (
    ALL_COLUMNS,
    GUIDE_VERSION,
    LABELER_COLS,
    dropdown_for_column,
    dropdown_lists,
    help_row,
)
from laminary_pipeline.ingest.wikidata import effective_series_status

TEMPLATE_NAME = "gold_labels_template.csv"
LISTS_NAME = "gold_labels_lists.csv"
README_NAME = "gold_labels_readme.csv"
TEXTS_DIR = "texts"
MANIFEST_NAME = "manifest.csv"
MANIFEST_COLUMNS = ["filename", "qid", "title", "sha256", "word_count"]


def text_file_name(qid: str) -> str:
    return f"{TEXTS_DIR}/{qid}.txt"

README_LINES = [
    f"Laminary gold labeling sheet (guide version {GUIDE_VERSION})",
    "Full guide: 'Laminary gold labeling guide.docx' (from docs/GOLD_LABELING_GUIDE.md).",
    "Prefer gold_labels.xlsx: it has the dropdowns, notes and shading built in. These CSVs",
    "are the fallback.",
    "",
    "1. Pick a row assigned to you. Put your labeler code in labeler_id. Some titles have",
    "   two rows (label_slot 1 and 2) for two different people: label on your own and",
    "   don't discuss the title or look at the other row until Hari says labeling is done.",
    "2. Open the file named in summary_text_file (in the 'texts' folder next to this sheet",
    "   in the Laminary Drive folder) and read ONLY that text. It is exactly",
    "   what the model reads. Don't label from the Wikipedia page (wikipedia_revision_link",
    "   is there for attribution) and don't use anything you know about the film or show.",
    "   If summary_coverage is filled (e.g. 'Summary covers seasons 1–4 of 7.'), the text",
    "   stops before the series does: label only what it covers and never judge an ending",
    "   you can't see (guide rule on partial coverage). The model gets the same line.",
    "   Some series files are episode summaries, one per paragraph ('S2E5 \"Title\": ...');",
    "   '(season 3 only in part; ...)' in summary_coverage means that season stops early.",
    "   A series without seasons may be covered by year ('Summary covers 1998–1999 of",
    "   1998–2025.', episodes marked '1998E5'): each year counts as a season.",
    "3. Fill every white column: primary_plot, the 9 plot_ columns (Y/N), blueprint,",
    "   the 12 stage_ columns (Y/N), arc_shape (or all 11 arc_t points), the 10 tag_",
    "   columns (Y/N) and confidence. notes is optional.",
    "4. If the summary is too thin to judge, or describes a different work, choose a",
    "   skip_reason instead and leave the labels blank.",
    "5. Flat story (no big rise or fall)? Use a 'Flat:' option in arc_shape.",
    "6. Don't edit grey (prefilled) columns and don't delete the # help row.",
    "",
    "Dropdowns: Data > Data validation > Dropdown (from a range) > Lists tab.",
    "Columns and their dropdown list:",
]


MULTI_SEP = " | "  # separates per-source values in the source_* columns


def _count(n: int, unit: str) -> str:
    """'1 season', '3 seasons' (QA nit 2026-10-03: not '1 seasons')."""
    return f"{n} {unit}{'' if n == 1 else 's'}"


def _gated(plot: dict[str, Any]) -> GatedInput:
    """One ok plot file's sources gated exactly like annotation input (Wikipedia-only, hashes,
    word counts, 150 words, season coverage). Raises GateError or InputFormatError if the
    model could not be sent this text."""
    sources = []
    for src in ingest_sources(plot):
        if not isinstance(src.get("text"), str):
            raise InputFormatError("plot file source without text")
        sources.append(PlotSource({k: v for k, v in src.items() if k != "text"}, src["text"]))
    cand = plot.get("candidate") or {}
    title = {"media_type": cand.get("media_type"), "tmdb_id": cand.get("tmdb_id")}
    origin = plot.get("qid", "plot file")
    total, basis = parse_coverage(plot.get("coverage"), sources, origin)
    via = ingest_via(plot)
    via_detail = parse_via_detail(plot, via, origin)
    partial = parse_partial_season(plot.get("coverage"), sources, via_detail, origin)
    span, partial_year = parse_year_coverage(plot.get("coverage"), sources, via_detail, origin)
    fetcher = plot.get("fetcher_version")
    gated = gate(PlotInput(title, tuple(sources), origin, via=via,
                           season_total=total, season_total_basis=basis,
                           fetcher_version=str(fetcher or "0"),
                           via_detail=via_detail, partial_season=partial,
                           year_span=span, partial_year=partial_year))
    # the same refusal as the request builder under the default prompt (QA 2026-10-02): a
    # plot file from before fetcher 1.4.0 never reaches the sheet
    if load_prompt(DEFAULT_PROMPT_VERSION).sends_coverage:
        require_current_ingest(gated.plot)
    return gated


def labeler_text_for(plot: dict[str, Any]) -> str:
    """The text file for one ok plot file (``prompt.labeler_text`` of the gated input)."""
    return labeler_text(_gated(plot))


def labeler_coverage_for(plot: dict[str, Any]) -> str:
    """The ``summary_coverage`` cell for one ok plot file (``prompt.labeler_coverage``)."""
    return labeler_coverage(_gated(plot))


def template_rows(
    selection: Sequence[dict[str, Any]], plots: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, str]], list[str]]:
    """Sheet rows for selected titles whose ok plot file passes the annotation gate; plus
    messages for the rest."""
    rows, skipped = [], []
    for sel in selection:
        plot = plots.get(sel["qid"])
        if not plot or plot.get("status") != "ok":
            skipped.append(f"{sel['qid']} {sel['title']!r}: no passing plot section yet")
            continue
        try:
            labeler_text_for(plot)
            coverage = labeler_coverage_for(plot)
            sources = ingest_sources(plot)
        except (GateError, InputFormatError, NotAnnotatable) as e:
            reason = "plot text doesn't match its sha256" if "sha256" in str(e) else str(e)
            skipped.append(f"{sel['qid']} {sel['title']!r}: {reason}")
            continue
        cand = plot.get("candidate", {})
        seasons = plot.get("via") == "season_articles"
        episodes = plot.get("via_detail") == "episode_table"
        links = [p["permalink"] for p in plot["sources"]] if seasons else [plot["permalink"]]
        unit = "year" if (plot.get("coverage") or {}).get("unit") == "year" else "season"

        def joined(field: str, sources: list[dict[str, Any]] = sources) -> str:
            return MULTI_SEP.join(str(src[field]) for src in sources)

        row = {c: "" for c in ALL_COLUMNS}
        row.update(
            qid=sel["qid"],
            title=sel["title"],
            year=str(sel["year"]),
            type=sel["media_type"],
            summary_text_file=text_file_name(sel["qid"]),
            wikipedia_revision_link=MULTI_SEP.join(links),
            plot_section=(
                f"Episode tables ({_count(len(sources), unit)})"
                if episodes
                else f"Season articles ({len(sources)})" if seasons
                else plot["section"]["heading"].capitalize()
            ),
            word_count=str(plot["word_count"]),
            summary_coverage=coverage,
            series_status=effective_series_status(cand) or "",
            tmdb_id=str(sel["tmdb_id"]),
            source_ref=joined("ref"),
            source_revision=joined("revision"),
            source_retrieved_at=joined("retrieved_at"),
            source_license=joined("license"),
            source_word_count=joined("word_count"),
            source_sha256=joined("content_sha256"),
            guide_version=GUIDE_VERSION,
            label_slot="1",
        )
        rows.append(row)
        if sel.get("double_label"):
            rows.append({**row, "label_slot": "2"})
    return rows, skipped


def _csv(rows: Sequence[Sequence[str]]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue()


def template_csv(rows: Sequence[dict[str, str]]) -> str:
    help_ = help_row()
    body = [ALL_COLUMNS, [help_.get(c, "") for c in ALL_COLUMNS]]
    body += [[r.get(c, "") for c in ALL_COLUMNS] for r in rows]
    return _csv(body)


def lists_csv() -> str:
    lists = dropdown_lists()
    names = list(lists)
    depth = max(len(v) for v in lists.values())
    body = [names] + [[lists[n][i] if i < len(lists[n]) else "" for n in names]
                      for i in range(depth)]
    return _csv(body)


def readme_csv() -> str:
    lines = list(README_LINES)
    for col in LABELER_COLS:
        dd = dropdown_for_column(col)
        lines.append(f"  {col}: {'dropdown ' + dd if dd else 'free entry'}")
    return _csv([[line] for line in lines])


def manifest_rows(
    rows: Sequence[dict[str, str]], plots: Mapping[str, dict[str, Any]]
) -> list[dict[str, str]]:
    """One manifest row per text file (double-labeled titles share one file)."""
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        qid = row["qid"]
        if qid in out:
            continue
        data = labeler_text_for(dict(plots[qid])).encode("utf-8")
        out[qid] = {
            "filename": text_file_name(qid).removeprefix(f"{TEXTS_DIR}/"),
            "qid": qid,
            "title": row["title"],
            "sha256": hashlib.sha256(data).hexdigest(),
            "word_count": row["word_count"],
        }
    return list(out.values())


def manifest_csv(rows: Sequence[dict[str, str]]) -> str:
    return _csv([MANIFEST_COLUMNS] + [[r[c] for c in MANIFEST_COLUMNS] for r in rows])


def write_template(
    out_dir: Path,
    rows: Sequence[dict[str, str]],
    plots: Mapping[str, dict[str, Any]] | None = None,
) -> list[Path]:
    """Write the three CSV tabs and, with ``plots``, ``texts/<QID>.txt`` for every title plus
    ``texts/manifest.csv``; stale ``texts/Q*.txt`` files are removed."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {TEMPLATE_NAME: template_csv(rows), LISTS_NAME: lists_csv(), README_NAME: readme_csv()}
    written = []
    for name, text in files.items():
        path = out_dir / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    if plots is not None:
        texts = out_dir / TEXTS_DIR
        texts.mkdir(parents=True, exist_ok=True)
        manifest = manifest_rows(rows, plots)
        keep = {m["filename"] for m in manifest}
        for stale in texts.glob("Q*.txt"):
            if stale.name not in keep:
                stale.unlink()
        for m in manifest:
            path = texts / m["filename"]
            path.write_bytes(labeler_text_for(dict(plots[m["qid"]])).encode("utf-8"))
            written.append(path)
        path = texts / MANIFEST_NAME
        path.write_text(manifest_csv(manifest), encoding="utf-8")
        written.append(path)
    return written
