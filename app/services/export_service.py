from __future__ import annotations

import json
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
from app.storage.local import LocalFileStorage

logger = logging.getLogger(__name__)
SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"s": SHEET_NS, "r": DOC_REL_NS, "p": PACKAGE_REL_NS}
ET.register_namespace("", SHEET_NS)
ET.register_namespace("r", DOC_REL_NS)


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


def _set_cell(row: ET.Element, column: int, row_number: int, value: object, text: bool) -> None:
    reference = f"{_column_name(column)}{row_number}"
    cells = list(row.findall("s:c", NS))
    cell = next((candidate for candidate in cells if candidate.get("r") == reference), None)
    if cell is None:
        cell = ET.Element(f"{{{SHEET_NS}}}c", {"r": reference})
        insert_at = next((index for index, candidate in enumerate(cells) if _column_index(candidate.get("r", "")) > column), len(cells))
        row.insert(insert_at, cell)
    for child in list(cell):
        cell.remove(child)
    if text:
        cell.set("t", "inlineStr")
        inline = ET.SubElement(cell, f"{{{SHEET_NS}}}is")
        ET.SubElement(inline, f"{{{SHEET_NS}}}t").text = str(value)
    else:
        cell.attrib.pop("t", None)
        ET.SubElement(cell, f"{{{SHEET_NS}}}v").text = str(value)


class ExportBlockedError(ValueError):
    pass


class ExportService:
    """Create output by patching only the three standardised columns in worksheet XML."""

    def __init__(self, repositories: MongoRepositories, storage: LocalFileStorage):
        self.repositories = repositories
        self.storage = storage

    def prepare_export(self, job_id: str) -> None:
        job = self.repositories.get_job(job_id)
        if not job:
            raise ValueError("job not found")
        if job.get("status") == "EXPORTED" and self.storage.get_output_path(job_id).exists():
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
        if job.get("status") == "EXPORTED" and self.storage.get_output_path(job_id).exists():
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
                worksheet = ET.fromstring(input_archive.read(worksheet_path))
                sheet_data = worksheet.find("s:sheetData", NS)
                if sheet_data is None:
                    raise ValueError(f"worksheet {INPUT_SHEET!r} contains no data")
                rows = {int(row.get("r", "0")): row for row in sheet_data.findall("s:row", NS)}
                header = rows.get(1)
                if header is None:
                    raise ValueError("worksheet header row is missing")
                headers = {_cell_value(cell, shared_strings).strip(): _column_index(cell.get("r", "")) for cell in header.findall("s:c", NS)}
                field_columns = {field: headers[FIELD_MAP[field]] for field in ("standard_size", "standard_uom", "standard_pack_size")}
                timer.done(worksheet_path=worksheet_path, row_count=len(rows))

                self.repositories.update_job(job_id, {"progress.stage": "PATCHING_KLM_FIELDS"})
                timer = _Timer("export.patch_cells", job_id=job_id)
                patched_cells = 0
                skipped_rows = 0
                for item in export_items:
                    review = item.get("review") or {}
                    if review.get("overall_status") == "REJECTED":
                        continue
                    if review.get("overall_status") == "OVERRIDDEN":
                        values = review.get("override_values") or {}
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
                timer.done(patched_cells=patched_cells, skipped_rows=skipped_rows)

                self.repositories.update_job(job_id, {"progress.stage": "SERIALIZING_WORKSHEET"})
                timer = _Timer("export.serialize_worksheet", job_id=job_id)
                worksheet_bytes = ET.tostring(worksheet, encoding="utf-8", xml_declaration=True)
                if not worksheet_bytes.startswith(b"<?xml"):
                    raise ValueError("generated worksheet XML is invalid")
                timer.done(byte_count=len(worksheet_bytes))

                self.repositories.update_job(job_id, {"progress.stage": "WRITING_ARCHIVE"})
                timer = _Timer("export.write_archive", job_id=job_id)
                with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as output_archive:
                    for entry in input_archive.infolist():
                        content = worksheet_bytes if entry.filename == worksheet_path else input_archive.read(entry.filename)
                        output_archive.writestr(entry, content)
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
            self.repositories.update_job(job_id, {"status": "EXPORTED", "output_storage_key": storage_key, "error": None, "progress.stage": "EXPORTED"})
            export_timer.done(status="EXPORTED")
            return self.storage.get_output_path(job_id)
        except Exception as exc:
            self.repositories.update_job(job_id, {"status": "FAILED", "progress.stage": "FAILED", "error": f"{type(exc).__name__}: {exc}"})
            _log_event("export.failed", job_id=job_id, error_type=type(exc).__name__, error=str(exc))
            raise
        finally:
            temporary.unlink(missing_ok=True)
