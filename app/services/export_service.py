from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import logging
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from time import perf_counter
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from app.domain.enums import BASE_UNITS
from app.repositories.mongo import MongoRepositories
from app.services.excel_reader import FIELD_MAP, INPUT_SHEET
from app.services.result_status import STATUS_LABELS, differs, effective_status, group_label, how_label, route_of
from app.storage.local import LocalFileStorage

logger = logging.getLogger(__name__)
SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"s": SHEET_NS, "r": DOC_REL_NS, "p": PACKAGE_REL_NS}
ET.register_namespace("", SHEET_NS)
ET.register_namespace("r", DOC_REL_NS)

_NAMESPACE_DECLARATION = re.compile(rb'xmlns:([A-Za-z_][\w.-]*)="([^"]+)"')
_WORKSHEET_ROOT = re.compile(rb"<worksheet\b[^>]*>")


def _adopt_source_namespaces(worksheet_xml: bytes) -> dict[str, str]:
    """Keep the workbook's own namespace prefixes when we re-serialize the sheet.

    ElementTree renames prefixes it does not know to ns0, ns1, ... But Excel writes
    ``mc:Ignorable="x14ac xr xr2 xr3"``, and those prefixes are plain text inside an
    attribute value, so renaming leaves them pointing at nothing. Excel then refuses
    the file as damaged. openpyxl does not validate this, so it stays invisible
    unless the workbook is opened in Excel itself.
    """
    root = _WORKSHEET_ROOT.search(worksheet_xml)
    declarations = {
        prefix.decode(): uri.decode()
        for prefix, uri in _NAMESPACE_DECLARATION.findall(root.group(0) if root else b"")
    }
    for prefix, uri in declarations.items():
        ET.register_namespace(prefix, uri)
    return declarations


def _restore_namespace_declarations(
    worksheet_xml: bytes, declarations: dict[str, str]
) -> bytes:
    """Put back declarations ElementTree dropped because no element used them.

    ``xr2`` and ``xr3`` are usually declared and never used, yet mc:Ignorable names
    them, so they have to survive the round trip.
    """
    root = _WORKSHEET_ROOT.search(worksheet_xml)
    if root is None:
        return worksheet_xml
    tag = root.group(0)
    missing = b"".join(
        f' xmlns:{prefix}="{uri}"'.encode()
        for prefix, uri in declarations.items()
        if b"xmlns:" + prefix.encode() + b"=" not in tag
    )
    if not missing:
        return worksheet_xml
    opening = len(b"<worksheet")
    patched = tag[:opening] + missing + tag[opening:]
    return worksheet_xml[: root.start()] + patched + worksheet_xml[root.end() :]

AUDIT_COLUMNS = (
    # The outcome, how it came about, what happened, and why: read left to right.
    "Group",
    "How",
    "Cleansing Status",
    "Comment",
    "Cleansing Changed Fields",
    "Original K",
    "Proposed K",
    "Final K",
    "Original L",
    "Proposed L",
    "Final L",
    "Original M",
    "Proposed M",
    "Final M",
    "Cleansing Method",
    "Review Status",
    "Reviewer Comment",
    # Technical, kept at the end for the delivery team.
    "Cleansing Finding Codes",
    "Cleansing Categories",
    "Cleansing Version",
)
STATUS_COLUMN = AUDIT_COLUMNS.index("Cleansing Status")

# The website check: our value against the retailer's page text and pack photo. Filled only
# for products that were checked; blank means not checked. Never touches K, L or M.
WEB_COLUMNS = ("Our value vs website", "Page text", "Pack photo", "Why", "Product page", "Pack photo link", "Checked on")
WEB_FILLS = {"MATCHES": "FFE6F4EA", "DIFFERS": "FFF4DEDE", "UNSETTLED": "FFFDF1D9"}

# A background per status, matching the colours used on the Agent performance screen.
STATUS_FILLS = {
    "INVALID": "FFF4DEDE",
    "NO_CHANGE": "FFE6F4EA",        # green  - already correct
    "AUTO_APPLY": "FFE4F0FA",       # blue   - a value changed
    "OBSERVATION_ONLY": "FFE2F4F2", # teal   - checked, noted, unchanged
    "REVIEW_REQUIRED": "FFFDF1D9",  # amber  - waiting for a person
    "UNRESOLVED": "FFF0EAFA",       # purple - left blank rather than guess
    "SKIPPED": "FFF1F3F5",          # grey   - purged, never processed
    "WEB_MATCHES": "FFDDF3E4", "WEB_DIFFERS": "FFF7D6D6", "WEB_UNSETTLED": "FFFBEBC9",  # the website check: green, red, amber
}

_MEASURES = {"GM": "a weight", "ML": "a volume", "EA": "a count", "FT": "a length"}
_SOURCE_NAMES = {
    "item_desc_eng": "the English description",
    "item_desc_local_lang": "the local-language description",
    "web_description_eng": "the English web description",
    "web_description_chi": "the local-language web description",
    "item_brand_eng": "the English brand",
    "item_brand_local_lang": "the local-language brand",
}
_FRAGMENT = re.compile(r"'fragment': '([^']*)'")
STYLES_PATH = "xl/styles.xml"

# Bumped whenever the workbook we produce changes: columns, wording, colour, or the XML
# we write. A job that was exported under an older version rebuilds on the next request
# instead of handing back a stale file, which is how a fixed export used to stay
# invisible to anyone who had already downloaded once.
EXPORT_VERSION = "export-v17"  # v17: one K L M blind test sheet per sample


def _log_event(event: str, **fields: object) -> None:
    logger.info(json.dumps({"event": event, **fields}, default=str, separators=(",", ":")))


class _Timer:
    def __init__(self, event: str, **fields: object):
        self.event = event
        self.fields = fields
        self.started = perf_counter()

    def done(self, **fields: object) -> None:
        elapsed = round((perf_counter() - self.started) * 1000)
        _log_event(self.event, **self.fields, **fields, duration_ms=elapsed)


def _excel_number(value: object) -> int | float | str | None:
    if value is None:
        return None
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    return int(decimal) if decimal == decimal.to_integral() else float(decimal)


def _column_index(reference: str) -> int:
    letters = "".join(character for character in reference if character.isalpha())
    value = 0
    for character in letters.upper():
        value = value * 26 + ord(character) - 64
    return value


def _column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _shared_strings(archive: ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return ["".join(node.text or "" for node in item.findall(".//s:t", NS)) for item in root]


def _cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    if cell.get("t") == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//s:t", NS))
    node = cell.find("s:v", NS)
    if node is None or node.text is None:
        return ""
    return shared_strings[int(node.text)] if cell.get("t") == "s" else node.text


def _worksheet_path(archive: ZipFile) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    sheet = next((node for node in workbook.findall("s:sheets/s:sheet", NS) if node.get("name") == INPUT_SHEET), None)
    if sheet is None:
        raise ValueError(f"required worksheet {INPUT_SHEET!r} is missing")
    relationship_id = sheet.get(f"{{{DOC_REL_NS}}}id")
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    relationship = next((node for node in relationships.findall("p:Relationship", NS) if node.get("Id") == relationship_id), None)
    if relationship is None or not relationship.get("Target"):
        raise ValueError(f"worksheet relationship for {INPUT_SHEET!r} is missing")
    target = relationship.get("Target", "").lstrip("/")
    return target if target.startswith("xl/") else str(PurePosixPath("xl") / target)


def _set_cell(row: ET.Element, column: int, row_number: int, value: object, text: bool,
              style: int | None = None, link: str | None = None) -> None:
    """``link`` writes a clickable HYPERLINK formula showing ``value``; no relationship part is needed."""
    reference = f"{_column_name(column)}{row_number}"
    cells = list(row.findall("s:c", NS))
    cell = next((candidate for candidate in cells if candidate.get("r") == reference), None)
    if cell is None:
        cell = ET.Element(f"{{{SHEET_NS}}}c", {"r": reference})
        insert_at = next((index for index, candidate in enumerate(cells) if _column_index(candidate.get("r", "")) > column), len(cells))
        row.insert(insert_at, cell)
    for child in list(cell):
        cell.remove(child)
    if style is not None:
        cell.set("s", str(style))
    if link:
        cell.set("t", "str")
        safe = str(link).replace('"', "%22")
        ET.SubElement(cell, f"{{{SHEET_NS}}}f").text = f'HYPERLINK("{safe}","{str(value).replace(chr(34), "")}")'
        ET.SubElement(cell, f"{{{SHEET_NS}}}v").text = str(value)
    elif text:
        cell.set("t", "inlineStr")
        inline = ET.SubElement(cell, f"{{{SHEET_NS}}}is")
        ET.SubElement(inline, f"{{{SHEET_NS}}}t").text = str(value)
    else:
        cell.attrib.pop("t", None)
        ET.SubElement(cell, f"{{{SHEET_NS}}}v").text = str(value)


# Elements that must follow <hyperlinks> in a worksheet, in schema order.
_AFTER_HYPERLINKS = ("printOptions", "pageMargins", "pageSetup", "headerFooter", "rowBreaks", "colBreaks", "customProperties",
                     "cellWatches", "ignoredErrors", "smartTags", "drawing", "legacyDrawing", "legacyDrawingHF", "picture",
                     "oleObjects", "controls", "webPublishItems", "tableParts", "extLst")


def _add_hyperlinks(worksheet: ET.Element, worksheet_path: str, archive: ZipFile,
                    links: list[tuple[str, str]]) -> tuple[str, bytes | None]:
    """Real hyperlink objects (clickable in every viewer, no calculation needed): one
    <hyperlink> per cell, each pointing at an external relationship of the sheet."""
    name = PurePosixPath(worksheet_path).name
    rels_path = f"xl/worksheets/_rels/{name}.rels"
    if not links:
        return rels_path, None
    if rels_path in archive.namelist():
        rels_text = archive.read(rels_path).decode("utf-8")
    else:
        rels_text = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{PACKAGE_REL_NS}"></Relationships>'
    existing = {int(m) for m in re.findall(r'Id="rId(\d+)"', rels_text)}
    next_id = max(existing, default=0) + 1
    container = worksheet.find("s:hyperlinks", NS)
    if container is None:
        container = ET.Element(f"{{{SHEET_NS}}}hyperlinks")
        children = list(worksheet)
        index = next((i for i, child in enumerate(children) if child.tag.split("}")[-1] in _AFTER_HYPERLINKS), len(children))
        worksheet.insert(index, container)
    added = []
    for ref, url in links:
        rid = f"rId{next_id}"
        next_id += 1
        safe = url.replace("&", "&amp;").replace('"', "%22")
        added.append(f'<Relationship Id="{rid}" Type="{DOC_REL_NS}/hyperlink" Target="{safe}" TargetMode="External"/>')
        ET.SubElement(container, f"{{{SHEET_NS}}}hyperlink", {"ref": ref, f"{{{DOC_REL_NS}}}id": rid})
    rels_text = rels_text.replace("</Relationships>", "".join(added) + "</Relationships>")
    return rels_path, rels_text.encode("utf-8")


_FILLS_COUNT = re.compile(r'<fills count="(\d+)">')
_FONTS_COUNT = re.compile(r'<fonts count="(\d+)"[^>]*>')
_CELL_XFS_COUNT = re.compile(r'<cellXfs count="(\d+)">')


def _add_status_styles(styles_xml: bytes) -> tuple[bytes, dict[str, int]]:
    """Add one background colour per status and return status -> style index.

    The workbook is patched as raw XML, so a colour means appending a fill to
    ``<fills>`` and a matching record to ``<cellXfs>``, then pointing the cell at that
    record. Both counts must be updated or Excel rejects the file.
    """
    text = styles_xml.decode("utf-8")
    fills, cell_xfs = _FILLS_COUNT.search(text), _CELL_XFS_COUNT.search(text)
    if not fills or not cell_xfs or "</fills>" not in text or "</cellXfs>" not in text:
        return styles_xml, {}  # unexpected shape: ship the workbook without colour
    first_fill, first_style = int(fills.group(1)), int(cell_xfs.group(1))
    added = len(STATUS_FILLS)

    text = text.replace(fills.group(0), f'<fills count="{first_fill + added}">', 1)
    text = text.replace("</fills>", "".join(
        f'<fill><patternFill patternType="solid"><fgColor rgb="{colour}"/>'
        f'<bgColor indexed="64"/></patternFill></fill>'
        for colour in STATUS_FILLS.values()
    ) + "</fills>", 1)

    text = text.replace(cell_xfs.group(0), f'<cellXfs count="{first_style + added}">', 1)
    text = text.replace("</cellXfs>", "".join(
        f'<xf numFmtId="0" fontId="0" fillId="{first_fill + offset}" borderId="0"'
        f' xfId="0" applyFill="1"/>'
        for offset in range(added)
    ) + "</cellXfs>", 1)

    styles = {status: first_style + offset for offset, status in enumerate(STATUS_FILLS)}
    # A link style (blue, underlined) so hyperlink cells look like links.
    fonts = _FONTS_COUNT.search(text)
    if fonts and "</fonts>" in text:
        font_id = int(fonts.group(1))
        text = text.replace(fonts.group(0), fonts.group(0).replace(f'count="{font_id}"', f'count="{font_id + 1}"'), 1)
        text = text.replace("</fonts>", '<font><u/><sz val="11"/><color rgb="FF0563C1"/><name val="Calibri"/><family val="2"/></font></fonts>', 1)
        text = text.replace(f'<cellXfs count="{first_style + added}">', f'<cellXfs count="{first_style + added + 1}">', 1)
        text = text.replace("</cellXfs>", f'<xf numFmtId="0" fontId="{font_id}" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>', 1)
        styles["LINK"] = first_style + added
    return text.encode("utf-8"), styles


def _plain(value: object) -> str:
    """Render a stored number the way a person writes it: 1000, not 1E+3 or 1000.0."""
    if value is None:
        return ""
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except (InvalidOperation, ValueError):
        return str(value)


def _measure(uom: str) -> str:
    kind = _MEASURES.get(uom.strip().upper())
    return f"{uom} ({kind})" if kind else uom


def _evidence(finding: dict[str, object]) -> tuple[str, str]:
    values = {
        str(row.get("role")): str(row.get("value") or "")
        for row in list(finding.get("evidence") or [])
        if isinstance(row, dict)
    }
    return values.get("CURRENT", ""), values.get("EXPECTED", "")


def _finding_comment(finding: dict[str, object], original: dict[str, object]) -> str:
    """One sentence a reviewer can act on, with the actual values in it.

    Written here rather than reused from the stored text because a spreadsheet cell has
    to carry the whole story on its own, where the screen can show a before/after panel
    beside it. Findings the pipeline already words well are passed through untouched.
    """
    code = str(finding.get("code") or "")
    current, expected = _evidence(finding)
    uom = str(original.get("standard_uom") or "")
    legacy = f"{_plain(original.get('legacy_size'))} {original.get('legacy_uom') or ''}".strip()
    source = _SOURCE_NAMES.get(str(finding.get("field") or ""), "the product description")

    if code == "SIGNIFICANT_LEGACY_SIZE_MISMATCH" and current and expected:
        # No conversion happened when the legacy unit already matches, so do not
        # write "(450 GM) works out to 450 GM".
        says = (
            f"the legacy data says {expected} {uom}"
            if legacy.startswith(f"{expected} ")
            else f"the legacy data ({legacy}) works out to {expected} {uom}"
        )
        return (
            f"Excel says {current} {uom}, but {says}. "
            "Check the pack and confirm which is right."
        )
    if code == "LEGACY_UOM_MISMATCH" and current and expected:
        kind = _MEASURES.get(expected.strip().upper(), "")
        return (
            f"Excel measures this in {_measure(current)}, but the legacy data says "
            f"{legacy}{f', which is {kind}' if kind else ''}. These measure different "
            "things, so confirm which one applies."
        )
    if code == "DESCRIPTION_SIZE_DIFFERS" and current:
        return (
            f"{source.capitalize()} says “{current}”, but Excel says {expected or uom}. "
            "Nothing was changed. Confirm which size is right."
        )
    if code == "DESCRIPTION_MEASUREMENT_MISMATCH" and current:
        return (
            f"{source.capitalize()} has “{current}”, a different kind of unit from Excel's "
            f"{expected or uom}; it is most likely part of the name. Nothing was changed."
        )
    if code == "PACKAGING_HIERARCHY_AMBIGUOUS":
        wording = _FRAGMENT.search(current)
        quoted = f"“{wording.group(1)}” in {source}" if wording else f"the wording in {source}"
        existing = (
            f"{_plain(original.get('standard_size'))} {uom} × "
            f"{_plain(original.get('standard_pack_size'))}"
        )
        return (
            f"{quoted} can be read more than one way, and the values in Excel ({existing}) "
            "match none of them. Confirm whether the size is per packet, per inner pack, "
            "or per case."
        )
    if code == "PACK_SIZE_SINGLE_ITEM":
        return (
            "No pack count is written anywhere, so the product is recorded as a single item "
            "with pack size 1."
        )
    if code == "LINKED_SIZE_AND_PACK_SUGGESTION" and current and expected:
        proposed = dict(finding.get("proposed") or {})
        suggestion = (
            f"{_plain(proposed.get('standard_size'))} {proposed.get('standard_uom') or uom} × "
            f"{_plain(proposed.get('standard_pack_size'))}"
        )
        return (
            f"Excel says {current}. The legacy data ({legacy}) is the size of one pack and the "
            f"description says “{expected}”: {suggestion} gives the same total. Suggested "
            f"{suggestion}; confirm how the pack is sold."
        )
    if code == "DESCRIPTION_PACK_COUNT_DIFFERS" and current and expected:
        return (
            f"The description says “{expected}”, but Excel has a pack size of {current}. "
            "Nothing was changed and nothing is suggested. Confirm which is current."
        )
    if code == "DESCRIPTION_CONFIRMS_PIECE_COUNT" and current and expected:
        legacy_unit = str(original.get("legacy_uom") or "").strip().upper()
        about_legacy = (
            f"The legacy data ({legacy}) gives a weight or volume, not a piece count, "
            "so it does not contradict this."
            if _MEASURES.get(legacy_unit) or legacy_unit in {"GM", "G", "KG", "ML", "L", "LT"}
            else f"The legacy data ({legacy}) is the package, not one piece."
        )
        return (
            f"The description says “{expected}”, which matches Excel's {current}. "
            f"{about_legacy} Nothing was changed."
        )
    if code == "DESCRIPTION_CONFIRMS_UNIT_SIZE" and current and expected:
        return (
            f"The description says “{expected}”, which matches Excel's {current}, so the "
            f"legacy data ({legacy}) was not used to change it. Nothing was changed."
        )
    if code == "LEGACY_TOTAL_CONSISTENT" and current and expected:
        return (
            f"The legacy data ({expected}) is the whole pack: {current} = {expected}. "
            "The values agree, so nothing was changed."
        )
    if code == "ROUNDING_ONLY_VARIANCE":
        return (
            "The value agrees with the legacy data once label rounding is allowed. "
            "Nothing was changed and no action is needed."
        )
    # Everything else is already written in plain language by the pipeline.
    return str(finding.get("human_reason") or "")


def _outcome_comment(item: dict[str, object], status: str, original: dict[str, object]) -> str:
    """What happened to the row, before any finding explains why."""
    if status == "SKIPPED":
        return (
            "This product record is empty, so it was skipped. Nothing was read or changed."
        )
    if status == "INVALID":
        return (
            "A value in this row cannot be used (for example text where a number should be, "
            "or a unit that is not in the table), so it could be neither checked nor filled in. "
            "Please correct the value."
        )
    if status == "UNRESOLVED" and route_of(item) == "B":
        unit = original.get("legacy_uom") or "the legacy unit"
        return (
            f"The legacy unit {unit} has no agreed conversion, so the size could not be "
            "filled in. It was left blank rather than guessed."
        )
    if status != "AUTO_APPLY":
        return ""
    legacy = f"{_plain(original.get('legacy_size'))} {original.get('legacy_uom') or ''}".strip()
    method = str(item.get("method") or "")
    source = (
        "the product description" if "AI_INFERENCE" in method
        else f"the legacy data ({legacy})" if legacy and route_of(item) == "B"
        else how_label(item).lower() if route_of(item) == "INCOMPLETE"
        else "the existing values"
    )
    moves = [
        f"{label} {_plain(change.get('original')) or 'blank'} → {_plain(change.get('proposed'))}"
        for field, label in (
            ("standard_size", "size"), ("standard_uom", "unit"), ("standard_pack_size", "pack size"),
        )
        for change in [next(
            (row for row in list(item.get("changes") or []) if row.get("field") == field), {},
        )]
        if change.get("proposed") is not None and differs(change.get("proposed"), change.get("original"))
    ]
    if not moves:
        return ""
    if route_of(item) == "INCOMPLETE":
        return f"Filled in the missing values ({source}): " + ", ".join(moves) + "."
    if route_of(item) == "B" and original.get("standard_size") not in (None, "") and "AI_INFERENCE" not in method:
        # Excel's own value was converted to a standard unit (16 OZ → 454 GM).
        return "Converted to a standard unit with the unit table: " + ", ".join(moves) + "."
    return f"Corrected from {source}: " + ", ".join(moves) + "."


def _comment(item: dict[str, object]) -> str:
    original = dict(item.get("original") or {})
    status = effective_status(item)
    lines = [_outcome_comment(item, status, original)]
    lines += [_finding_comment(finding, original) for finding in list(item.get("findings") or [])]
    written = list(dict.fromkeys(line for line in lines if line))
    if written:
        return " | ".join(written)
    return "Checked. The existing values passed every check."


# The same wording is shown on screen when two runs are compared.
row_comment = _comment


def _audit_values(item: dict[str, object]) -> tuple[str, ...]:
    findings = list(item.get("findings") or [])
    changes = {
        change.get("field"): change
        for change in list(item.get("changes") or [])
    }
    review = dict(item.get("review") or {})
    proposals = dict(item.get("field_proposals") or {})
    status = effective_status(item)
    review_status = str(review.get("overall_status") or "NOT_REQUIRED")
    overrides = dict(review.get("override_values") or {})

    def values(field: str) -> tuple[str, str, str]:
        change = dict(changes.get(field) or {})
        original = change.get("original")
        proposed = change.get("proposed", proposals.get(field))
        if review_status == "OVERRIDDEN" and overrides.get(field) is not None:
            final = overrides[field]
        elif review_status == "APPROVED" and proposed is not None:
            final = proposed
        elif status == "AUTO_APPLY" and proposed is not None:
            final = proposed
        else:
            final = original
        return tuple("" if value is None else str(value) for value in (original, proposed, final))

    k = values("standard_size")
    l = values("standard_uom")
    m = values("standard_pack_size")
    changed_fields = [
        field for field, change in changes.items()
        if change.get("proposed") is not None and differs(change.get("proposed"), change.get("original"))
    ]
    return (
        group_label(item),
        how_label(item),
        STATUS_LABELS.get(status, status),
        _comment(item),
        "; ".join(changed_fields),
        *k,
        *l,
        *m,
        str(item.get("method") or "NONE"),
        review_status,
        str(review.get("comment") or ""),
        "; ".join(str(row.get("code") or "") for row in findings),
        "; ".join(sorted({str(row.get("category") or "") for row in findings if row.get("category")})),
        str(item.get("result_ledger_version") or ""),
    )


class ExportBlockedError(ValueError):
    pass


class ExportService:
    """Create output by patching only the three standardised columns in worksheet XML."""

    def __init__(self, repositories: MongoRepositories, storage: LocalFileStorage):
        self.repositories = repositories
        self.storage = storage

    def _already_current(self, job: dict[str, object], job_id: str) -> bool:
        """True when the file on disk was built by this version of the exporter."""
        current = (
            job.get("status") == "EXPORTED"
            and job.get("export_version") == EXPORT_VERSION
            and self.storage.get_output_path(job_id).exists()
        )
        if current and hasattr(self.repositories, "web_evidence_run"):
            run = self.repositories.web_evidence_run(job_id) or {}
            finished, exported = run.get("finished_at"), job.get("exported_at")
            if finished and (exported is None or finished > exported):
                return False
        if current and hasattr(self.repositories, "bc_image_blind_test_for_workbook"):
            run = self.repositories.bc_image_blind_test_for_workbook(job_id) or {}
            finished, exported = run.get("finished_at"), job.get("exported_at")
            if finished and (exported is None or finished > exported
                             or job.get("image_blind_source_job_id") != run.get("job_id")):
                return False
        if current and hasattr(self.repositories, "klm_blind_test"):
            # A test finished, or a reviewer's answer saved, after the export makes the file stale.
            test = self.repositories.klm_blind_test(job_id) or {}
            saved, exported = test.get("saved_at"), job.get("exported_at")
            if test.get("status") == "READY" and saved and (exported is None or saved > exported):
                return False
        return current

    def prepare_export(self, job_id: str) -> None:
        job = self.repositories.get_job(job_id)
        if not job:
            raise ValueError("job not found")
        if self._already_current(job, job_id):
            return
        if job.get("status") == "EXPORTING":
            raise ExportBlockedError("workbook export is already running")
        self.repositories.update_job(job_id, {"status": "EXPORTING", "error": None, "progress.stage": "PREPARING_EXPORT"})

    def export_in_background(self, job_id: str) -> None:
        try:
            self.export(job_id)
        except Exception:
            logger.exception("Workbook export failed for job %s", job_id)

    def export(self, job_id: str) -> Path:
        job = self.repositories.get_job(job_id)
        if not job:
            raise ValueError("job not found")
        if self._already_current(job, job_id):
            _log_event("export.idempotent_return", job_id=job_id)
            return self.storage.get_output_path(job_id)
        source = self.storage.get_input_path(job_id)
        with NamedTemporaryFile(suffix=".xlsx", delete=False) as handle:
            temporary = Path(handle.name)
        export_timer = _Timer("export.completed", job_id=job_id)
        try:
            self.repositories.update_job(job_id, {"progress.stage": "FETCHING_PROPOSALS"})
            timer = _Timer("export.fetch_items", job_id=job_id)
            export_items = self.repositories.export_items(job_id) if hasattr(self.repositories, "export_items") else self.repositories.all_items(job_id)
            timer.done(item_count=len(export_items))

            self.repositories.update_job(job_id, {"progress.stage": "READING_WORKBOOK"})
            with ZipFile(source, "r") as input_archive:
                timer = _Timer("export.read_workbook", job_id=job_id)
                worksheet_path = _worksheet_path(input_archive)
                shared_strings = _shared_strings(input_archive)
                source_worksheet = input_archive.read(worksheet_path)
                namespaces = _adopt_source_namespaces(source_worksheet)
                worksheet = ET.fromstring(source_worksheet)
                sheet_data = worksheet.find("s:sheetData", NS)
                if sheet_data is None:
                    raise ValueError(f"worksheet {INPUT_SHEET!r} contains no data")
                rows = {int(row.get("r", "0")): row for row in sheet_data.findall("s:row", NS)}
                header = rows.get(1)
                if header is None:
                    raise ValueError("worksheet header row is missing")
                headers = {_cell_value(cell, shared_strings).strip(): _column_index(cell.get("r", "")) for cell in header.findall("s:c", NS)}
                field_columns = {field: headers[FIELD_MAP[field]] for field in ("standard_size", "standard_uom", "standard_pack_size")}
                audit_start = max(headers.values()) + 1
                for offset, audit_header in enumerate(AUDIT_COLUMNS):
                    _set_cell(header, audit_start + offset, 1, audit_header, text=True)
                web_start = audit_start + len(AUDIT_COLUMNS)
                web_rows = {int(d["row_number"]): d for d in self.repositories.web_evidence(job_id)} if hasattr(self.repositories, "web_evidence") else {}
                if web_rows:
                    for offset, web_header in enumerate(WEB_COLUMNS):
                        _set_cell(header, web_start + offset, 1, web_header, text=True)
                timer.done(worksheet_path=worksheet_path, row_count=len(rows))

                styles_bytes, status_styles = _add_status_styles(
                    input_archive.read(STYLES_PATH)
                ) if STYLES_PATH in input_archive.namelist() else (b"", {})

                self.repositories.update_job(job_id, {"progress.stage": "PATCHING_KLM_FIELDS"})
                timer = _Timer("export.patch_cells", job_id=job_id)
                patched_cells = 0
                skipped_rows = 0
                hyperlinks: list[tuple[str, str]] = []
                for item in export_items:
                    review = item.get("review") or {}
                    if review.get("overall_status") == "REJECTED":
                        continue
                    if review.get("overall_status") == "OVERRIDDEN":
                        values = review.get("override_values") or {}
                    elif (
                        item.get("application_policy") == "REVIEW_REQUIRED"
                        and review.get("overall_status") != "APPROVED"
                    ):
                        # Download remains available, but unapproved human-review
                        # proposals must not mutate the workbook.
                        values = {}
                    else:
                        values = item.get("field_proposals") or {}
                    row_number = int(item["row_number"])
                    row = rows.get(row_number)
                    if row is None:
                        skipped_rows += 1
                        _log_event(
                            "export.row_missing",
                            job_id=job_id,
                            row_number=row_number,
                            item_no=item.get("item_no"),
                        )
                        continue
                    for field, column in field_columns.items():
                        value = values.get(field)
                        if value is None:
                            continue
                        if field == "standard_uom":
                            value = str(value).upper()
                            if value not in BASE_UNITS:
                                raise ValueError(f"invalid final UOM at row {row_number}: {value}")
                            _set_cell(row, column, row_number, value, text=True)
                        else:
                            _set_cell(row, column, row_number, _excel_number(value), text=False)
                        patched_cells += 1
                    audit = _audit_values(item)
                    for offset, value in enumerate(audit):
                        _set_cell(
                            row,
                            audit_start + offset,
                            row_number,
                            value,
                            text=True,
                            style=(
                                status_styles.get(effective_status(item))
                                if offset == STATUS_COLUMN else None
                            ),
                        )
                        patched_cells += 1
                    checked = web_rows.get(row_number)
                    if checked:
                        from app.services.web_evidence_service import evaluate  # noqa: PLC0415 - avoids an import cycle
                        e = evaluate(checked)
                        source = checked.get("source") or {}
                        when = checked.get("checked_at")
                        when_text = f"{when.strftime('%d %b %Y') if hasattr(when, 'strftime') else str(when)[:10]} · {source.get('site') or 'wellcome.com.hk'}"
                        photos = source.get("image_urls") or []
                        cells = [(e["status_label"], status_styles.get("WEB_" + e["status"]), None), (e["page_cell"], None, None),
                                 (e["photo_cell"], None, None), (e["why"], None, None),
                                 (source.get("page_url"), None, source.get("page_url")), (e.get("evidence_photo"), None, e.get("evidence_photo")),
                                 (when_text, None, None)]
                        for offset, (value, style, link) in enumerate(cells):
                            if value:
                                _set_cell(row, web_start + offset, row_number, value, text=True,
                                          style=status_styles.get("LINK") if link else style)
                            if link:
                                hyperlinks.append((f"{_column_name(web_start + offset)}{row_number}", link))
                            patched_cells += 1
                last_column = _column_name(audit_start + len(AUDIT_COLUMNS) + (len(WEB_COLUMNS) if web_rows else 0) - 1)
                dimension = worksheet.find("s:dimension", NS)
                if dimension is not None:
                    dimension.set("ref", f"A1:{last_column}{max(rows)}")
                # The source sheet carries an AutoFilter over its own columns. If it is
                # left as-is, Excel sorts and filters only those columns and the audit
                # block stays put, so one sort detaches every status from its row.
                # Widening the range keeps the audit columns moving with the data.
                for auto_filter in worksheet.findall("s:autoFilter", NS):
                    original = auto_filter.get("ref", "")
                    if ":" in original:
                        auto_filter.set("ref", f"{original.split(':')[0]}:{last_column}{max(rows)}")
                rels_path, rels_bytes = _add_hyperlinks(worksheet, worksheet_path, input_archive, hyperlinks)
                timer.done(patched_cells=patched_cells, skipped_rows=skipped_rows, hyperlinks=len(hyperlinks))

                self.repositories.update_job(job_id, {"progress.stage": "SERIALIZING_WORKSHEET"})
                timer = _Timer("export.serialize_worksheet", job_id=job_id)
                worksheet_bytes = _restore_namespace_declarations(
                    ET.tostring(worksheet, encoding="utf-8", xml_declaration=True),
                    namespaces,
                )
                if not worksheet_bytes.startswith(b"<?xml"):
                    raise ValueError("generated worksheet XML is invalid")
                timer.done(byte_count=len(worksheet_bytes))

                self.repositories.update_job(job_id, {"progress.stage": "WRITING_ARCHIVE"})
                timer = _Timer("export.write_archive", job_id=job_id)
                with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as output_archive:
                    replacements = {worksheet_path: worksheet_bytes}
                    if status_styles:
                        replacements[STYLES_PATH] = styles_bytes
                    if rels_bytes:
                        replacements[rels_path] = rels_bytes
                    # A finished sample test becomes its own sheet beside the data.
                    sample_test = self.repositories.sample_test(job_id) if hasattr(self.repositories, "sample_test") else None
                    extra: dict[str, bytes] = {}
                    from app.services.sample_test_sheet import add_sheet  # noqa: PLC0415 - avoids an import cycle
                    if sample_test and sample_test.get("status") == "READY":
                        from app.services.sample_test_sheet import sheet_xml  # noqa: PLC0415 - avoids an import cycle
                        parts = {name: replacements.get(name) or input_archive.read(name)
                                 for name in ("xl/workbook.xml", "xl/_rels/workbook.xml.rels", "[Content_Types].xml")}
                        extra = add_sheet(parts, sheet_xml(sample_test))
                        replacements.update({k: v for k, v in extra.items() if k in parts})
                    # Always expose the test in the delivered workbook. Before it is run,
                    # the sheet states that clearly instead of silently disappearing.
                    from app.services.bc_image_blind_sheet import SHEET_NAME as IMAGE_SHEET_NAME, sheet_xml as image_sheet_xml  # noqa: PLC0415
                    if hasattr(self.repositories, "bc_image_blind_test_for_workbook"):
                        image_run = self.repositories.bc_image_blind_test_for_workbook(job_id)
                    else:
                        image_run = self.repositories.bc_image_blind_test(job_id) if hasattr(self.repositories, "bc_image_blind_test") else None
                    image_source_job = image_run.get("job_id") if image_run and image_run.get("status") == "READY" else None
                    image_rows = self.repositories.bc_image_blind_rows(image_source_job, 0, 100000)[0] if image_source_job else []
                    current_parts = {name: replacements.get(name) or input_archive.read(name)
                                     for name in ("xl/workbook.xml", "xl/_rels/workbook.xml.rels", "[Content_Types].xml")}
                    image_extra = add_sheet(current_parts, image_sheet_xml(image_run, image_rows),
                                            sheet_name=IMAGE_SHEET_NAME, file_prefix="image_blind_test")
                    replacements.update({k: v for k, v in image_extra.items() if k in current_parts})
                    extra.update({k: v for k, v in image_extra.items() if k not in current_parts})
                    # The K L M blind test is always a sheet, with the reviewer's Yes/No last. It is a
                    # test of this run's own output, so only this run's test fills it; before one
                    # has run, the sheet says so instead of being silently absent.
                    from app.services.klm_blind_sheet import SHEET_NAME as KLM_SHEET_NAME, sheet_name as klm_sheet_name, sheet_xml as klm_sheet_xml  # noqa: PLC0415
                    from app.services.klm_blind_test_service import sample_numbers as klm_samples  # noqa: PLC0415
                    klm_test = self.repositories.klm_blind_test(job_id) if hasattr(self.repositories, "klm_blind_test") else None
                    klm_ready = bool(klm_test and klm_test.get("status") == "READY")
                    # One sheet per sample ("KLM Blind Test 1", "2", ...); a single "not run" sheet before any.
                    klm_sheets = [(klm_sheet_name(n), klm_sheet_xml(klm_test, sample=n)) for n in klm_samples(klm_test)] if klm_ready else []
                    for klm_name, klm_bytes in klm_sheets or [(KLM_SHEET_NAME, klm_sheet_xml(None))]:
                        current_parts = {name: replacements.get(name) or input_archive.read(name)
                                         for name in ("xl/workbook.xml", "xl/_rels/workbook.xml.rels", "[Content_Types].xml")}
                        klm_extra = add_sheet(current_parts, klm_bytes, sheet_name=klm_name, file_prefix="klm_blind_test")
                        replacements.update({k: v for k, v in klm_extra.items() if k in current_parts})
                        extra.update({k: v for k, v in klm_extra.items() if k not in current_parts})
                    for entry in input_archive.infolist():
                        content = replacements.get(entry.filename) or input_archive.read(entry.filename)
                        output_archive.writestr(entry, content)
                    for name, content in extra.items():
                        if name not in replacements:
                            output_archive.writestr(name, content)
                    if rels_bytes and rels_path not in input_archive.namelist():
                        output_archive.writestr(rels_path, rels_bytes)
                timer.done(entry_count=len(input_archive.infolist()))

            self.repositories.update_job(job_id, {"progress.stage": "VALIDATING_OUTPUT"})
            timer = _Timer("export.validate_output", job_id=job_id)
            with ZipFile(temporary, "r") as output_archive:
                output_archive.getinfo("xl/workbook.xml")
                output_archive.getinfo(worksheet_path)
            timer.done()

            self.repositories.update_job(job_id, {"progress.stage": "STORING_OUTPUT"})
            timer = _Timer("export.store_output", job_id=job_id)
            storage_key = self.storage.save_output(job_id, temporary)
            timer.done(storage_key=storage_key)
            self.repositories.update_job(job_id, {
                "status": "EXPORTED", "output_storage_key": storage_key, "error": None,
                "export_version": EXPORT_VERSION, "progress.stage": "EXPORTED", "exported_at": datetime.now(timezone.utc),
                "image_blind_source_job_id": image_source_job,
            })
            export_timer.done(status="EXPORTED")
            return self.storage.get_output_path(job_id)
        except Exception as exc:
            self.repositories.update_job(job_id, {"status": "FAILED", "progress.stage": "FAILED", "error": f"{type(exc).__name__}: {exc}"})
            _log_event("export.failed", job_id=job_id, error_type=type(exc).__name__, error=str(exc))
            raise
        finally:
            temporary.unlink(missing_ok=True)
