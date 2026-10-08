"""The "KLM Blind Test" worksheet added to the delivered workbook: a short summary, then one
line per sampled row with our values, the blind checker's, the result, and, last, an empty
Yes/No for a human reviewer (a drop-down)."""
from __future__ import annotations

from typing import Any

from app.services.klm_blind_test_service import QUESTION, rows_of, sample_numbers, say, summarize
from app.services.sample_test_sheet import _cell, _col

SHEET_NAME = "KLM Blind Test"
REVIEWER_HEADER = "Reviewer: are our K L M right? (Yes / No)"
COLUMNS = (
    ("Group", 8), ("Excel row", 10), ("Item No", 11), ("Product", 34), ("Product (local)", 24), ("Category", 20),
    ("Old size", 12), ("Uploaded K", 11), ("Uploaded L", 10), ("Uploaded M", 11),
    ("What we did", 22), ("Our K", 10), ("Our L", 8), ("Our M", 8),
    ("Checker's answer", 20), ("Checker K", 10), ("Checker L", 9), ("Checker M", 9),
    ("Result", 12), ("Kind of error", 18), ("Why", 60), ("Checker's reason", 60), ("Quoted from the file", 36),
    ("Reviewer comment", 36), (REVIEWER_HEADER, 24),
)
_RESULT = {"RIGHT": "Right", "WRONG": "Wrong", "CANT_TELL": "Can't tell"}
_DECISION = {"AS_IS": "Right as it stands", "VALUES": "These values", "NEEDS_PERSON": "A person is needed"}
_ANSWER = {"YES": "Yes", "NO": "No"}


def _v(values: dict[str, Any] | None, field: str) -> Any:
    return (values or {}).get(field)


def _pct(f: dict[str, Any]) -> Any:
    return f["percent"] if f.get("percent") is not None else "—"


def sheet_name(sample: int) -> str:
    return f"{SHEET_NAME} {sample}"


def lines_of(test: dict[str, Any] | None, bold: int = 1, sample: int | None = None) -> tuple[list[list[Any]], list[int], int]:
    """``sample`` gives that sample's sheet; without it, every row of the test."""
    lines: list[list[Any]] = []
    styles: list[int] = []

    def add(cells: list[Any], style: int = 0) -> None:
        lines.append(cells)
        styles.append(style)

    if not test or test.get("status") != "READY":
        add(["K L M blind test · not run"], bold)
        add(["Run the K L M blind test from the Accuracy tab, then download the workbook again."])
        add([])
        add([c for c, _ in COLUMNS], bold)
        return lines, styles, len(lines)
    s = summarize(test, sample)
    rows = rows_of(test, sample)
    numbers = sample_numbers(test)
    seed = (test.get("seeds") or {}).get(str(sample)) if sample else None
    which = f"sample {sample} of {len(numbers)} · " if sample and len(numbers) > 1 else ""
    add([f"K L M blind test · {which}{len(rows)} rows drawn at random, {test.get('size')} from each group · seed {seed or test.get('seed')}"], bold)
    if sample and len(numbers) > 1:
        every = summarize(test)["overall"]
        add([f"The samples share no row: each is a separate random draw of rows the others did not take. "
             f"All {len(numbers)} samples together: {every['right']} of {every['checked']} right = {every['percent']}% "
             f"(range {every['low']}–{every['high']}%)."])
    add(["Question: are the K (unit size), L (unit) and M (pack size) in our output right? A second, stronger AI model worked out the right "
         "values for each row from the raw data alone. It was not shown our output or the row's group. Plain rules then compared the two."])
    add(["Right = the checker reaches the same amount in the same unit. Wrong = it reaches a different one. Can't tell = the checker finds the "
         "file cannot settle the values, or could not answer; these rows are left out of the figure, never counted as right. "
         "Accuracy = rows right ÷ rows checked."])
    add([])
    add(["Group", "Question", "Sample", "Checked", "Right", "Wrong", "Can't tell", "Accuracy %", "Range (95%)", "Errors", "Reviewer accuracy %", "Reviewer marked"], bold)
    for g in s["groups"]:
        errors = ", ".join(f"{label}: {g['errors'][key]}" for key, label in (
            ("MISSED_ERROR", "missed error"), ("WRONG_CORRECTION", "wrong correction"), ("FALSE_ALARM", "false alarm"), ("WRONG_SUGGESTION", "wrong suggestion")) if g["errors"].get(key))
        add([g["group"], QUESTION[g["group"]], g["sample"], g["checked"], g["right"], g["wrong"], g["cant_tell"], _pct(g),
             f"{g['low']}–{g['high']}" if g["percent"] is not None else "—", errors or "none", _pct(g["person"]), g["person"]["checked"]])
    o = s["overall"]
    add(["All", "All rows checked, the three groups in equal numbers", o["sample"], o["checked"], o["right"], o["wrong"], o["cant_tell"], _pct(o),
         f"{o['low']}–{o['high']}" if o["percent"] is not None else "—", "", _pct(s["person"]), s["person"]["checked"]])
    if s.get("weighted_percent") is not None:
        add(["", f"Weighted by each group's share of the workbook: {s['weighted_percent']}% (Group A is most of the rows)"])
    add([f"Checker: {(rows[0].get('check') or {}).get('model_id') if rows else ''} · {test.get('checker_version')}. "
         "The last column is for a human reviewer: choose Yes or No. Where the reviewer and the checker differ, the reviewer's answer stands."])
    add([])
    header_row = len(lines) + 1
    add([c for c, _ in COLUMNS], bold)
    for r in rows:
        check, person = r.get("check") or {}, r.get("person") or {}
        ours = r.get("suggestion") if r["group"] == "C" else r.get("final")
        did = r.get("action") or ""
        if r["group"] == "C":
            did += f" (suggested {say(r['suggestion'])})" if r.get("suggestion") else " (no suggestion)"
        quoted = "; ".join(f"“{e['fragment']}” ({e['field']})" for e in check.get("evidence") or [])
        add([r["group"], r.get("row_number"), r.get("item_no"), r.get("product"), r.get("product_local"), r.get("category"), r.get("legacy"),
             _v(r.get("excel"), "standard_size"), _v(r.get("excel"), "standard_uom"), _v(r.get("excel"), "standard_pack_size"),
             did, _v(ours, "standard_size"), _v(ours, "standard_uom"), _v(ours, "standard_pack_size"),
             _DECISION.get(check.get("decision") or "", "No answer"),
             _v(check.get("values"), "standard_size"), _v(check.get("values"), "standard_uom"), _v(check.get("values"), "standard_pack_size"),
             _RESULT.get(r.get("verdict"), ""), r.get("error_label") or "", r.get("why"), check.get("reason") or check.get("error"), quoted,
             person.get("comment") or "", _ANSWER.get(person.get("answer") or "", "")])
    return lines, styles, header_row


def sheet_xml(test: dict[str, Any] | None, bold: int = 0, sample: int | None = None) -> bytes:
    lines, styles, header_row = lines_of(test, bold, sample)
    body = []
    for i, (cells, style) in enumerate(zip(lines, styles), 1):
        body.append(f'<row r="{i}">' + "".join(_cell(f"{_col(j)}{i}", v, style) for j, v in enumerate(cells)) + "</row>")
    cols = "".join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>' for i, (_, w) in enumerate(COLUMNS, 1))
    last_col = _col(len(COLUMNS) - 1)
    last = f"{last_col}{max(1, len(lines))}"
    # A Yes/No drop-down on the reviewer column, for every data row.
    validation = ""
    if len(lines) > header_row:
        validation = ('<dataValidations count="1"><dataValidation type="list" allowBlank="1" showErrorMessage="1" '
                      f'errorTitle="Yes or No" error="Choose Yes or No" sqref="{last_col}{header_row + 1}:{last_col}{len(lines)}">'
                      '<formula1>"Yes,No"</formula1></dataValidation></dataValidations>')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<dimension ref="A1:{last}"/><sheetViews><sheetView workbookViewId="0">'
            f'<pane ySplit="{header_row}" topLeftCell="A{header_row + 1}" activePane="bottomLeft" state="frozen"/>'
            f'</sheetView></sheetViews><cols>{cols}</cols><sheetData>{"".join(body)}</sheetData>'
            f'<autoFilter ref="A{header_row}:{last}"/>{validation}</worksheet>').encode("utf-8")
