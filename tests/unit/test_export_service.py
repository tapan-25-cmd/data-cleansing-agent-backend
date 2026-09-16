from io import BytesIO
from uuid import uuid4

from openpyxl import Workbook, load_workbook

from app.services.excel_reader import FIELD_MAP, INPUT_SHEET
from app.services.export_service import ExportService
from app.storage.local import LocalFileStorage


class RepositoriesStub:
    def __init__(self) -> None:
        self.job = {"job_id": "test", "status": "READY_FOR_REVIEW"}
        self.updates: list[dict] = []
        self.items = [{
            "row_number": 2,
            "field_proposals": {"standard_size": "1000", "standard_uom": "GM", "standard_pack_size": "2"},
            "review": {"overall_status": "PENDING", "override_values": None},
        }]

    def get_job(self, _job_id: str) -> dict:
        return self.job

    def update_job(self, _job_id: str, values: dict) -> None:
        self.updates.append(values)
        self.job.update({key: value for key, value in values.items() if "." not in key})

    def all_items(self, _job_id: str) -> list[dict]:
        return self.items


def test_export_applies_pending_proposals_without_rebuilding_workbook(tmp_path):
    job_id = str(uuid4())
    storage = LocalFileStorage(tmp_path, 10_000_000)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = INPUT_SHEET
    headers = list(FIELD_MAP.values())
    sheet.append(headers)
    row = [None] * len(headers)
    row[headers.index(FIELD_MAP["item_no"])] = "SKU-1"
    row[headers.index(FIELD_MAP["legacy_size"])] = 1
    row[headers.index(FIELD_MAP["legacy_uom"])] = "KG"
    row[headers.index(FIELD_MAP["standard_size"])] = 1
    row[headers.index(FIELD_MAP["standard_uom"])] = "KG"
    row[headers.index(FIELD_MAP["standard_pack_size"])] = 1
    sheet.append(row)
    validated_row = [None] * len(headers)
    validated_row[headers.index(FIELD_MAP["item_no"])] = "SKU-A"
    validated_row[headers.index(FIELD_MAP["standard_size"])] = 500.5
    validated_row[headers.index(FIELD_MAP["standard_uom"])] = "ML"
    validated_row[headers.index(FIELD_MAP["standard_pack_size"])] = 1
    sheet.append(validated_row)
    source = BytesIO()
    workbook.save(source)
    source.seek(0)
    storage.save_input(job_id, source)
    repositories = RepositoriesStub()
    service = ExportService(repositories, storage)

    service.prepare_export(job_id)
    output = service.export(job_id)

    result = load_workbook(output, read_only=True, data_only=True)
    output_sheet = result[INPUT_SHEET]
    output_headers = {cell.value: cell.column for cell in output_sheet[1]}
    assert output_sheet.cell(2, output_headers[FIELD_MAP["legacy_size"]]).value == 1
    assert output_sheet.cell(2, output_headers[FIELD_MAP["legacy_uom"]]).value == "KG"
    assert output_sheet.cell(2, output_headers[FIELD_MAP["standard_size"]]).value == 1000
    assert output_sheet.cell(2, output_headers[FIELD_MAP["standard_uom"]]).value == "GM"
    assert output_sheet.cell(2, output_headers[FIELD_MAP["standard_pack_size"]]).value == 2
    # Export patches only proposal rows. A validated Group A row remains byte-for-byte
    # equivalent at the cell-value level, including its existing decimal size.
    assert output_sheet.cell(3, output_headers[FIELD_MAP["standard_size"]]).value == 500.5
    assert output_sheet.cell(3, output_headers[FIELD_MAP["standard_uom"]]).value == "ML"
    assert output_sheet.cell(3, output_headers[FIELD_MAP["standard_pack_size"]]).value == 1
    assert result.sheetnames == [INPUT_SHEET]
    result.close()
    assert repositories.job["status"] == "EXPORTED"
