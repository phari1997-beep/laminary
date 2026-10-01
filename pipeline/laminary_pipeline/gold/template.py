"""Write the gold labeling sheet as CSV files for Google Sheets.

Three files (one per tab; File > Import > "Insert new sheet(s)" for each):

- ``gold_labels_template.csv`` -> tab "Labels": one row per title, a help row, then the titles.
  Prefilled columns carry the Wikipedia revision the labeler must read and the source fields the
  importer needs (so the sheet is self-contained).
- ``gold_labels_lists.csv`` -> tab "Lists": one column per dropdown. In "Labels", use Data >
  Data validation > "Dropdown (from a range)" pointing at these columns.
- ``gold_labels_readme.csv`` -> tab "README": short instructions and the column guide.
- ``texts/<QID>.txt``: the exact summary text the model receives for each title (the plot
  file's ``text``, byte for byte, UTF-8, no trailing newline), so its SHA-256 equals the
  sheet's ``source_sha256``. Labelers read this file, not the Wikipedia page: the page also has
  tables, captions, hatnotes and notes that ingest strips (``ingest/text.py``), and a TV
  series' episode tables are dropped. The revision link stays in the sheet for attribution.
  These files hold CC BY-SA text and live under the gitignored data directory.

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


def text_file_name(qid: str) -> str:
    return f"{TEXTS_DIR}/{qid}.txt"

README_LINES = [
    f"Laminary gold labeling sheet (guide version {GUIDE_VERSION})",
    "Full guide: docs/GOLD_LABELING_GUIDE.md (Hari will share it as a Google Doc).",
    "",
    "1. Pick a row assigned to you. Put your labeler code in labeler_id. Some titles have",
    "   two rows (label_slot 1 and 2) for two different people: label on your own and",
    "   don't discuss the title or look at the other row until Hari says labeling is done.",
    "2. Open the file named in summary_text_file and read ONLY that text. It is exactly",
    "   what the model reads. Don't label from the Wikipedia page (wikipedia_revision_link",
    "   is there for attribution) and don't use anything you know about the film or show.",
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


def template_rows(
    selection: Sequence[dict[str, Any]], plots: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, str]], list[str]]:
    """Sheet rows for selected titles that have an ok plot file; plus messages for the rest."""
    rows, skipped = [], []
    for sel in selection:
        plot = plots.get(sel["qid"])
        if not plot or plot.get("status") != "ok":
            skipped.append(f"{sel['qid']} {sel['title']!r}: no passing plot section yet")
            continue
        src = plot["source"]
        text = plot.get("text")
        if not isinstance(text, str) or (
            hashlib.sha256(text.encode("utf-8")).hexdigest() != src.get("content_sha256")
        ):
            skipped.append(f"{sel['qid']} {sel['title']!r}: plot text doesn't match its sha256")
            continue
        cand = plot.get("candidate", {})
        row = {c: "" for c in ALL_COLUMNS}
        row.update(
            qid=sel["qid"],
            title=sel["title"],
            year=str(sel["year"]),
            type=sel["media_type"],
            summary_text_file=text_file_name(sel["qid"]),
            wikipedia_revision_link=plot["permalink"],
            plot_section=plot["section"]["heading"].capitalize(),
            word_count=str(plot["word_count"]),
            series_status=effective_series_status(cand) or "",
            tmdb_id=str(sel["tmdb_id"]),
            source_ref=src["ref"],
            source_revision=src["revision"],
            source_retrieved_at=src["retrieved_at"],
            source_license=src["license"],
            source_word_count=str(src["word_count"]),
            source_sha256=src["content_sha256"],
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


def write_template(
    out_dir: Path,
    rows: Sequence[dict[str, str]],
    plots: Mapping[str, dict[str, Any]] | None = None,
) -> list[Path]:
    """Write the three CSV tabs and, for every row, ``texts/<QID>.txt`` from ``plots``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {TEMPLATE_NAME: template_csv(rows), LISTS_NAME: lists_csv(), README_NAME: readme_csv()}
    written = []
    for name, text in files.items():
        path = out_dir / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    if plots is not None:
        for row in rows:
            if row.get("label_slot") == "2":
                continue  # same text file as slot 1
            qid = row["qid"]
            path = out_dir / text_file_name(qid)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(plots[qid]["text"].encode("utf-8"))
            written.append(path)
    return written
