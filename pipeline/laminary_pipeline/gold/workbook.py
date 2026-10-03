"""The gold labeling sheet as a formatted .xlsx workbook (``gold_labels.xlsx``).

Same rows and columns as ``gold_labels_template.csv`` (same order, same values), plus what a
CSV can't carry:

- **Labels** tab: a group row ("Title", "Plot", ...) above the column header; the CSV's ``#``
  help row becomes a note on each header cell and an input message on each labeler cell.
  Header rows and the title columns (``qid`` to ``label_slot``) are frozen. Prefilled columns
  are tinted and locked (sheet protection, no password); labeler columns are white and
  unlocked. Every labeler column with a fixed value set has a dropdown (data validation from a
  range on the Lists tab, never an inline list, which Excel caps at 255 characters) that
  rejects other entries; arc points accept a number from -1 to 1. Slot-2 rows (the second
  labeler of a double-labeled title) have a Dye-tinted band on their prefilled cells and a
  Dye "2" in ``label_slot``.
- **Lists** tab: the dropdown values, the same as ``gold_labels_lists.csv``; protected.
- **Read me** tab: how to use the sheet, the guide's file name, and the Google Sheets notes.

Every prefilled value is written as a text cell with the Text number format, so ``year``,
``source_retrieved_at`` or a value starting with ``=`` is never read as a number, date or
formula (QA 2026-10-03: Google Sheets converts those on CSV import).

Colours are the approved light-mode tokens (DECISIONS 2026-09-29, landing page CSS) and flat
tints of them; no gradients. ``openpyxl`` is the optional ``gold`` extra, imported lazily.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from laminary_pipeline.gold.columns import (
    ALL_COLUMNS,
    ARC_POINT_COLS,
    BOOKER_PLOTS,
    GUIDE_VERSION,
    LABELER_COLS,
    PLOT_COLS,
    PLOT_NAMES,
    PREFILLED_LEFT,
    PREFILLED_RIGHT,
    STAGE_COLS,
    STAGE_NAMES,
    STAGES,
    TAG_COLS,
    TAG_NAMES,
    TAGS,
    dropdown_for_column,
    dropdown_lists,
    help_row,
)

if TYPE_CHECKING:
    from openpyxl.worksheet.worksheet import Worksheet

XLSX_NAME = "gold_labels.xlsx"
GUIDE_DOCX_NAME = "Laminary gold labeling guide.docx"
LABELS_SHEET = "Labels"
LISTS_SHEET = "Lists"
README_SHEET = "Read me"
GROUP_ROW, HEADER_ROW, FIRST_DATA_ROW = 1, 2, 3
FROZEN_THROUGH = "label_slot"  # title/identity columns stay visible when scrolling right

# Approved tokens, light mode (DECISIONS 2026-09-29; laminary.lovable.app :root)
SCREEN = "F2F3EF"
INK = "17191E"
GRAIN = "555B63"
GATE = "7E848B"
DYE = "B0175C"
ALERT = "A2380C"
WHITE = "FFFFFF"
# flat tints of the tokens (token over white)
GATE_LINE = "D4D6D8"  # Gate at ~30%: cell borders
DYE_TINT = "F7E8EF"  # Dye at ~10%: slot-2 band
HEAD_FONT = "Archivo Narrow"
BODY_FONT = "Atkinson Hyperlegible Next"

GROUPS: list[tuple[str, list[str]]] = [
    ("Title", PREFILLED_LEFT[:PREFILLED_LEFT.index(FROZEN_THROUGH) + 1]),
    ("Summary to read", PREFILLED_LEFT[PREFILLED_LEFT.index(FROZEN_THROUGH) + 1:]),
    ("Labeler", ["labeler_id", "skip_reason"]),
    ("Plot", ["primary_plot", *PLOT_COLS]),
    ("Blueprint", ["blueprint", *STAGE_COLS]),
    ("Arc", ["arc_shape", *ARC_POINT_COLS]),
    ("Tags", TAG_COLS),
    ("Confidence and notes", ["confidence", "notes"]),
    ("Source details for the importer (don't edit)", PREFILLED_RIGHT),
]

WIDTHS = {
    "qid": 11, "title": 26, "year": 6, "type": 9, "label_slot": 9,
    "summary_text_file": 17, "wikipedia_revision_link": 50, "plot_section": 15,
    "word_count": 10.5, "summary_coverage": 42,
    "labeler_id": 9, "skip_reason": 22, "primary_plot": 24, "blueprint": 19,
    "arc_shape": 29, "confidence": 11, "notes": 36,
    "series_status": 9, "tmdb_id": 9, "source_ref": 40, "source_revision": 12,
    "source_retrieved_at": 21, "source_license": 13, "source_word_count": 9,
    "source_sha256": 24, "guide_version": 8,
}
YN_WIDTH, ARC_POINT_WIDTH = 4.6, 5.6
ROTATED = set(PLOT_COLS) | set(STAGE_COLS) | set(TAG_COLS) | set(ARC_POINT_COLS)
WRAPPED = {"title", "wikipedia_revision_link", "summary_coverage", "notes", "plot_section"}


def _require_openpyxl() -> Any:
    try:
        import openpyxl
    except ImportError as e:  # pragma: no cover - the gold extra is installed in CI
        raise RuntimeError(
            "the .xlsx sheet needs openpyxl: pip install -e '.[gold]' in pipeline/"
        ) from e
    return openpyxl


def short_name(column: str) -> str:
    """Readable name for input-message titles (Excel caps them at 32 characters)."""
    for cols, keys, names in ((PLOT_COLS, BOOKER_PLOTS, PLOT_NAMES),
                              (STAGE_COLS, STAGES, STAGE_NAMES), (TAG_COLS, TAGS, TAG_NAMES)):
        if column in cols:
            return names[keys[cols.index(column)]][:32]
    if column in ARC_POINT_COLS:
        return f"Arc point at {ARC_POINT_COLS.index(column) * 10}%"
    return column.replace("_", " ").capitalize()[:32]


def header_notes() -> dict[str, str]:
    """Per-column help, from the CSV's help row (minus its '#' marker text)."""
    notes = dict(help_row())
    notes["qid"] = "Wikidata id of the title (filled in; don't edit)"
    notes["label_slot"] = (
        "1, or 2 for the second labeler of a title two people label (rows tinted and marked 2). "
        "Take only the slot Hari assigns you, label on your own, and don't look at the other "
        "slot's row"
    )
    notes["summary_text_file"] = (
        "READ THIS FILE ONLY, from the texts folder next to this sheet: the exact summary text "
        "the model sees (its sha256 is source_sha256)"
    )
    return notes


def list_ranges() -> dict[str, str]:
    """Absolute range on the Lists tab for each dropdown, e.g. ``'Lists'!$A$2:$A$3``."""
    from openpyxl.utils import get_column_letter

    out = {}
    for i, (name, values) in enumerate(dropdown_lists().items(), start=1):
        letter = get_column_letter(i)
        out[name] = f"{LISTS_SHEET}!${letter}$2:${letter}${len(values) + 1}"
    return out


def _text(cell: Any, value: str) -> None:
    """A text cell that no spreadsheet reads as a number, date or formula."""
    cell.value = value
    cell.data_type = "s"
    cell.number_format = "@"


def readme_lines() -> list[tuple[str, str]]:
    """(style, text) lines of the Read me tab; style is title, head, body or alert."""
    return [
        ("title", "Laminary gold labeling sheet"),
        ("body", f"Guide version {GUIDE_VERSION}. Read the guide first: \"{GUIDE_DOCX_NAME}\", "
                 "in the same Drive folder as this sheet."),
        ("head", "How to use this sheet"),
        ("body", "1. Labels tab: one row per title. Label only the rows Hari assigned to you, "
                 "and put your labeler code (e.g. L01) in labeler_id."),
        ("body", "2. Read only the text file named in summary_text_file (texts folder). Don't "
                 "label from the Wikipedia page or from what you know about the title."),
        ("body", "3. White columns are yours: pick from each dropdown (Y/N, plot, blueprint, "
                 "arc, tags, confidence). Shaded columns are filled in; don't edit them."),
        ("body", "4. A tinted row with a 2 in label_slot is a second, independent label of a "
                 "title. Don't look at the title's other row or discuss the title."),
        ("body", "5. Can't label from the text? Choose a skip_reason and leave the labels blank. "
                 "Flat story? Use a 'Flat:' option in arc_shape."),
        ("body", "6. Hover over a column header for what it means. When you're done, tell Hari; "
                 "he downloads the sheet as .xlsx for import."),
        ("head", "Google Sheets"),
        ("body", "Dropdowns, notes and formatting carry over when this .xlsx is opened in Google "
                 "Sheets. Sheet protection may not."),
        ("alert", "Hari: before sharing, protect the shaded columns in Sheets with Data > "
                  "Protect sheets and ranges (the guide's 'For Hari' section has the steps)."),
        ("body", "The summary_text_file links work in Excel or Numbers next to the texts "
                 "folder. In Drive, open the file with that name from the texts folder."),
    ]


def write_workbook(path: Path, rows: Sequence[dict[str, str]]) -> Path:
    """Write ``gold_labels.xlsx``: Read me, Labels and Lists tabs. Returns ``path``."""
    openpyxl = _require_openpyxl()
    wb = openpyxl.Workbook()
    readme = wb.active
    readme.title = README_SHEET
    _readme_sheet(readme)
    labels = wb.create_sheet(LABELS_SHEET)
    _labels_sheet(labels, rows)
    lists = wb.create_sheet(LISTS_SHEET)
    _lists_sheet(lists)
    wb.active = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    wb.save(tmp)
    tmp.replace(path)
    return path


def _readme_sheet(ws: Worksheet) -> None:
    from openpyxl.styles import Alignment, Font

    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 3
    ws.column_dimensions["B"].width = 110
    fonts = {
        "title": Font(name=HEAD_FONT, size=18, bold=True, color=INK),
        "head": Font(name=HEAD_FONT, size=13, bold=True, color=INK),
        "body": Font(name=BODY_FONT, size=11, color=INK),
        "alert": Font(name=BODY_FONT, size=11, bold=True, color=ALERT),
    }
    row = 2
    for style, text in readme_lines():
        if style == "head":
            row += 1
        cell = ws.cell(row=row, column=2)
        _text(cell, text)
        cell.font = fonts[style]
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        row += 1
    ws.protection.sheet = True


def _lists_sheet(ws: Worksheet) -> None:
    from openpyxl.styles import Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    lists = dropdown_lists()
    head_font = Font(name=HEAD_FONT, bold=True, color=SCREEN)
    head_fill = PatternFill("solid", fgColor=INK)
    body_font = Font(name=BODY_FONT, color=INK)
    line = Border(bottom=Side(style="thin", color=GATE_LINE))
    for i, (name, values) in enumerate(lists.items(), start=1):
        head = ws.cell(row=1, column=i)
        _text(head, name)
        head.font, head.fill = head_font, head_fill
        for j, v in enumerate(values, start=2):
            cell = ws.cell(row=j, column=i)
            _text(cell, v)
            cell.font, cell.border = body_font, line
        ws.column_dimensions[get_column_letter(i)].width = max(
            12, max(len(v) for v in [name, *values]) + 3)
    ws.freeze_panes = "A2"
    ws.protection.sheet = True


def _labels_sheet(ws: Worksheet, rows: Sequence[dict[str, str]]) -> None:
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Protection, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    col_of = {c: i for i, c in enumerate(ALL_COLUMNS, start=1)}
    letter = {c: get_column_letter(i) for c, i in col_of.items()}
    last_row = FIRST_DATA_ROW + len(rows) - 1
    labeler = set(LABELER_COLS)

    def fill(color: str) -> PatternFill:
        return PatternFill("solid", fgColor=color)

    thin = Side(style="thin", color=GATE_LINE)
    grid = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_border = Border(left=thin, right=thin, top=thin,
                         bottom=Side(style="medium", color=INK))

    # group row
    ws.row_dimensions[GROUP_ROW].height = 22
    for name, cols in GROUPS:
        first, last = col_of[cols[0]], col_of[cols[-1]]
        cell = ws.cell(row=GROUP_ROW, column=first)
        _text(cell, name)
        prefilled = cols[0] not in labeler
        for c in range(first, last + 1):
            ws.cell(row=GROUP_ROW, column=c).fill = fill(GRAIN if prefilled else INK)
        cell.font = Font(name=HEAD_FONT, size=11, bold=True, color=SCREEN)
        cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        if last > first:
            ws.merge_cells(start_row=GROUP_ROW, start_column=first,
                           end_row=GROUP_ROW, end_column=last)

    # header row, with each column's help as a note
    notes = header_notes()
    ws.row_dimensions[HEADER_ROW].height = 178
    for c, i in col_of.items():
        cell = ws.cell(row=HEADER_ROW, column=i)
        _text(cell, c)
        mine = c in labeler
        cell.font = Font(name=HEAD_FONT, size=10, bold=True, color=INK if mine else GRAIN)
        cell.fill = fill(WHITE if mine else SCREEN)
        cell.border = head_border
        cell.alignment = Alignment(
            text_rotation=90 if c in ROTATED else 0, wrap_text=c not in ROTATED,
            horizontal="center" if c in ROTATED else "left", vertical="bottom")
        if notes.get(c):
            comment = Comment(notes[c], "Laminary")
            comment.width, comment.height = 320, 120
            cell.comment = comment
        width = WIDTHS.get(c)
        if width is None:
            width = ARC_POINT_WIDTH if c in ARC_POINT_COLS else YN_WIDTH
        ws.column_dimensions[letter[c]].width = width

    # data rows
    body = Font(name=BODY_FONT, size=10, color=INK)
    muted = Font(name=BODY_FONT, size=10, color=GRAIN)
    link = Font(name=BODY_FONT, size=10, color=INK, underline="single")
    slot_two = Font(name=BODY_FONT, size=10, bold=True, color=DYE)
    unlocked, locked = Protection(locked=False), Protection(locked=True)
    for r, row in enumerate(rows, start=FIRST_DATA_ROW):
        second = row.get("label_slot") == "2"
        for c, i in col_of.items():
            cell = ws.cell(row=r, column=i)
            value = row.get(c, "")
            cell.border = grid
            centered = c in ROTATED or c in ("year", "label_slot", "word_count", "confidence")
            cell.alignment = Alignment(
                horizontal="center" if centered else "left", vertical="top",
                wrap_text=c in WRAPPED)
            if c in labeler:
                cell.fill, cell.font, cell.protection = fill(WHITE), body, unlocked
                if value:
                    cell.value = value
                if c not in ARC_POINT_COLS:
                    cell.number_format = "@"
                continue
            _text(cell, value)
            cell.fill = fill(DYE_TINT if second else SCREEN)
            cell.protection = locked
            cell.font = body if c in ("qid", "title") else muted
            if c == "label_slot" and second:
                cell.font = slot_two
            if c == "summary_text_file" and value:
                cell.hyperlink = value
                cell.font = link
            if c == "wikipedia_revision_link" and value and " | " not in value:
                cell.hyperlink = value
                cell.font = link

    # dropdowns and input messages on every labeler column
    if rows:
        ranges = list_ranges()
        for c in LABELER_COLS:
            sqref = f"{letter[c]}{FIRST_DATA_ROW}:{letter[c]}{last_row}"
            dv = _validation(DataValidation, c, ranges, notes.get(c, ""))
            if dv is None:
                continue
            dv.add(sqref)
            ws.add_data_validation(dv)

    end = get_column_letter(len(ALL_COLUMNS))
    ws.auto_filter.ref = f"A{HEADER_ROW}:{end}{max(last_row, HEADER_ROW)}"
    ws.freeze_panes = f"{get_column_letter(col_of[FROZEN_THROUGH] + 1)}{FIRST_DATA_ROW}"
    ws.sheet_view.zoomScale = 100
    # no password; labelers may still resize, filter and select. Google Sheets may drop xlsx
    # protection: Hari protects the ranges there (GOLD_LABELING_GUIDE, "For Hari")
    ws.protection.sheet = True
    ws.protection.formatColumns = False
    ws.protection.formatRows = False
    ws.protection.autoFilter = False


def _validation(cls: Any, column: str, ranges: dict[str, str], help_text: str) -> Any:
    title = short_name(column)
    prompt = help_text[:255]
    dd = dropdown_for_column(column)
    if dd is not None:
        allowed = "Y or N" if dd == "yes_no" else f"a value from the list ({LISTS_SHEET} tab)"
        return cls(type="list", formula1=ranges[dd], allow_blank=True, showDropDown=False,
                   showErrorMessage=True, errorStyle="stop", errorTitle="Not in the list",
                   error=f"Choose {allowed}.", showInputMessage=True,
                   promptTitle=title, prompt=prompt)
    if column in ARC_POINT_COLS:
        return cls(type="decimal", operator="between", formula1="-1", formula2="1",
                   allow_blank=True, showErrorMessage=True, errorStyle="stop",
                   errorTitle="Arc point", error="Enter a number from -1 to 1.",
                   showInputMessage=True, promptTitle=title, prompt=prompt)
    if column == "labeler_id":
        return cls(type=None, allow_blank=True, showInputMessage=True,
                   promptTitle=title, prompt=prompt)
    return None


# ---------- reading a filled workbook ----------


def cell_text(value: Any) -> str:
    """A cell value as the text a CSV export would hold."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    return str(value)


def read_workbook_rows(path: Path) -> list[list[str]]:
    """Every row of the Labels tab (or, failing that, the first tab with a ``qid`` header) as
    text cells, from spreadsheet row 1. Formulas are read as their cached values."""
    openpyxl = _require_openpyxl()
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        by_name = {ws.title.strip().casefold(): ws for ws in wb.worksheets}
        ws = by_name.get(LABELS_SHEET.casefold())
        candidates = [ws] if ws is not None else list(wb.worksheets)
        for sheet in candidates:
            rows = [[cell_text(v) for v in r] for r in sheet.iter_rows(values_only=True)]
            if any("qid" in (c.strip().lower() for c in r) for r in rows[:5]):
                return rows
        return []
    finally:
        wb.close()
