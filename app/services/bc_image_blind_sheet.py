"""The B/C image blind-test worksheet added to the normal export."""
from __future__ import annotations

from typing import Any

from app.services.sample_test_sheet import _cell, _col

SHEET_NAME = "Image Blind Test"
COLUMNS = (
    ("Excel row", 11), ("Item No", 12), ("Group", 8),
    ("Uploaded K", 12), ("Uploaded L", 10), ("Uploaded M", 12),
    ("Proposed K", 12), ("Proposed L", 10), ("Proposed M", 12),
    ("Final K", 12), ("Final L", 10), ("Final M", 12),
    ("Image K", 12), ("Image L", 10), ("Image M", 12),
    ("Status", 30), ("Business category", 34), ("Responsible source", 28),
    ("Recommended action", 38), ("Clear explanation", 65), ("Comparable", 12), ("Technical reason", 55),
    ("Printed on pack", 30), ("Evidence photo", 44), ("Product page", 44),
    ("Images read", 12), ("Other-product photos", 20), ("Reader version", 22),
)


def _v(values: dict[str, Any] | None, field: str) -> Any:
    return (values or {}).get(field)


def sheet_xml(run: dict[str, Any] | None, rows: list[dict[str, Any]], bold: int = 0) -> bytes:
    lines: list[list[Any]] = []
    styles: list[int] = []
    def add(values: list[Any], style: int = 0) -> None:
        lines.append(values); styles.append(style)
    if not run or run.get("status") != "READY":
        add(["B/C Image Blind Test · Not run"], bold)
        add(["Run the Image blind test from the quality screen, then generate the workbook again."])
        header_row = 4
        add([]); add([c for c, _ in COLUMNS], bold)
    else:
        groups = run.get("groups") or {}
        add([f"B/C Image Blind Test · {run.get('population', len(rows))} eligible products"], bold)
        add(["The image AI saw product gallery images and a number-free identity hint. It did not see uploaded, proposed, or final K/L/M. Agreement is independent photo evidence, not business-labelled ground truth."])
        add(["Group", "Eligible", "Comparable", "Exact K/L/M", "Exact agreement %"], bold)
        for group in ("B", "C"):
            g = groups.get(group) or {}
            add([group, g.get("eligible", 0), g.get("comparable", 0), g.get("exact", 0), g.get("exact_percent")])
        add([])
        header_row = len(lines) + 1
        add([c for c, _ in COLUMNS], bold)
    for row in rows:
        blind, score = row.get("blind") or {}, row.get("primary_comparison") or row.get("final_comparison") or {}
        audit = row.get("interpretation") or {}
        source = row.get("source") or {}
        add([row.get("row_number"), row.get("item_no"), row.get("group"),
             _v(row.get("uploaded"), "standard_size"), _v(row.get("uploaded"), "standard_uom"), _v(row.get("uploaded"), "standard_pack_size"),
             _v(row.get("proposal"), "standard_size"), _v(row.get("proposal"), "standard_uom"), _v(row.get("proposal"), "standard_pack_size"),
             _v(row.get("final"), "standard_size"), _v(row.get("final"), "standard_uom"), _v(row.get("final"), "standard_pack_size"),
             blind.get("size"), blind.get("uom"), blind.get("count"), score.get("verdict"),
             audit.get("category"), audit.get("owner"), audit.get("action"), audit.get("explanation"),
             "Yes" if score.get("comparable") else "No", score.get("reason"), blind.get("printed"),
             source.get("evidence_photo"), source.get("page_url"), blind.get("images_read"),
             ", ".join(str(x) for x in blind.get("other_product_photos") or []), row.get("reader_version")])
    body = []
    for i, (cells, style) in enumerate(zip(lines, styles), 1):
        body.append(f'<row r="{i}">' + "".join(_cell(f"{_col(j)}{i}", value, style) for j, value in enumerate(cells)) + "</row>")
    cols = "".join(f'<col min="{i}" max="{i}" width="{width}" customWidth="1"/>' for i, (_, width) in enumerate(COLUMNS, 1))
    last = f"{_col(len(COLUMNS) - 1)}{max(1, len(lines))}"
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<dimension ref="A1:{last}"/><sheetViews><sheetView workbookViewId="0">'
            f'<pane ySplit="{header_row}" topLeftCell="A{header_row + 1}" activePane="bottomLeft" state="frozen"/>'
            f'</sheetView></sheetViews><cols>{cols}</cols><sheetData>{"".join(body)}</sheetData>'
            f'<autoFilter ref="A{header_row}:{last}"/></worksheet>').encode("utf-8")
