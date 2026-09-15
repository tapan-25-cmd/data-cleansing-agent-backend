from pathlib import Path

from openpyxl import Workbook

from app.agents.mock_provider import MockInferenceProvider
from app.agents.provider import (
    InferenceResponse,
    InferenceResult,
    ObservedMeasurement,
    ProviderMetadata,
)
from app.rules.registry import load_default_registry
from app.services.excel_reader import FIELD_MAP, INPUT_SHEET, PURGE_HEADERS
from app.services.processor import JobProcessor
from app.storage.local import LocalFileStorage


class FakeRepositories:
    def __init__(self, job):
        self.job = job
        self.items = []

    def get_job(self, job_id):
        return self.job if self.job["job_id"] == job_id else None

    def update_job(self, job_id, values):
        for key, value in values.items():
            if "." not in key:
                self.job[key] = value

    def replace_items(self, documents):
        self.items.extend(documents)


def create_fixture(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = INPUT_SHEET
    headers = sorted(set(FIELD_MAP.values()) | PURGE_HEADERS)
    sheet.append(headers)

    def add(**values):
        sheet.append([values.get(header) for header in headers])

    add(Item_no="000001", Department="03_Grocery 2")  # purged
    add(Item_no="000002", Department="03_Grocery 2", item_desc_eng="JUICE 500ML", **{
        "Standardize Unit Size": 500, "Standardize UOM": "ML", "Standardize Pack Size": 1,
    })
    add(Item_no="000003", Department="03_Grocery 2", item_desc_eng="FLOUR", item_size_value=1, item_size_unit="KG", **{
        "Standardize Unit Size": 1, "Standardize UOM": "KG",
    })
    add(Item_no="000004", Department="06_Dairy & Frozen", item_desc_eng="GREEN TEA 1 KG")
    add(Item_no="000005", Department="Other", item_desc_eng="OUT OF SCOPE")
    workbook.save(path)


def test_processor_routes_rules_and_ai_without_crossing_paths(tmp_path: Path):
    storage = LocalFileStorage(tmp_path / "files", 10_000_000)
    job_id = "abc123"
    fixture = tmp_path / "fixture.xlsx"
    create_fixture(fixture)
    with fixture.open("rb") as source:
        storage.save_input(job_id, source)
    repository = FakeRepositories({
        "job_id": job_id,
        "selected_departments": ["03_Grocery 2", "06_Dairy & Frozen"],
        "snapshot_label": None,
    })
    processor = JobProcessor(repository, storage, load_default_registry(), MockInferenceProvider())
    processor.process(job_id)

    assert repository.job["status"] == "READY_FOR_REVIEW"
    assert repository.job["stats"] == {
        "workbook_rows": 5, "department_rows": 4, "purged": 1, "live": 3,
        "group_a": 1, "group_b": 1, "group_b1": 1, "group_b2": 0,
        "group_c": 1, "data_shape_error": 0, "discrepancies": 0,
    }
    by_item = {item["item_no"]: item for item in repository.items}
    assert by_item["000003"]["method"] == "RULE"
    assert by_item["000003"]["field_proposals"]["standard_size"] == "1000"
    assert by_item["000004"]["method"] == "AI_INFERENCE"
    assert by_item["000004"]["reason_code"] == "NOT_IN_DESCRIPTION"
    assert by_item["000004"]["ai_provenance"]["provider"] == "mock"


class ExtractingProvider:
    async def infer(self, request):
        return InferenceResponse(
            result=InferenceResult(
                status="PROPOSAL",
                measurement=ObservedMeasurement(
                    value="1",
                    uom="KG",
                    field="item_desc_eng",
                    fragment="1 KG",
                ),
                confidence="HIGH",
                reason_code="EXPLICIT_MEASUREMENT",
            ),
            metadata=ProviderMetadata(
                provider="google-adk",
                agent_name="test-agent",
                agent_version="1",
            ),
        )


def test_ai_observation_is_converted_only_by_deterministic_rule_engine(tmp_path: Path):
    storage = LocalFileStorage(tmp_path / "files", 10_000_000)
    job_id = "def456"
    fixture = tmp_path / "fixture.xlsx"
    create_fixture(fixture)
    with fixture.open("rb") as source:
        storage.save_input(job_id, source)
    repository = FakeRepositories({
        "job_id": job_id,
        "selected_departments": ["03_Grocery 2", "06_Dairy & Frozen"],
        "snapshot_label": None,
    })

    JobProcessor(repository, storage, load_default_registry(), ExtractingProvider()).process(job_id)

    item = next(item for item in repository.items if item["item_no"] == "000004")
    assert item["ai_observation"] == {
        "value": "1", "uom": "KG", "field": "item_desc_eng", "fragment": "1 KG"
    }
    assert item["field_proposals"]["standard_size"] == "1000"
    assert item["field_proposals"]["standard_uom"] == "GM"
    assert item["rule"]["rule_id"] == "KG_TO_GM"
