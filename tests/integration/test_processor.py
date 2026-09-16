from pathlib import Path

from openpyxl import Workbook

from app.agents.mock_provider import MockInferenceProvider
from app.agents.provider import (
    Evidence,
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
        "group_b3": 0, "group_c": 1, "validation_review": 0,
        "group_a_validation_warnings": 0,
        "data_shape_error": 0, "discrepancies": 0,
        "pack_existing_valid": 0, "pack_normalized_existing": 0,
        "pack_deterministic_proposed": 0, "pack_agent_proposed": 0,
        "pack_agent_declined": 1, "pack_conflict": 0, "pack_not_found": 1,
        "pack_agent_error": 0, "pack_agent_disabled": 0,
        "pack_invalid_existing": 0,
        "group_b_pack_deterministic_proposed": 0,
        "group_b_pack_agent_proposed": 0,
        "group_c_pack_deterministic_proposed": 0,
        "group_c_pack_agent_proposed": 0,
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


def test_group_a_gate_normalizes_aliases_and_rejects_invalid_values(tmp_path: Path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = INPUT_SHEET
    headers = sorted(set(FIELD_MAP.values()) | PURGE_HEADERS)
    sheet.append(headers)

    def add(**values):
        sheet.append([values.get(header) for header in headers])

    add(Item_no="000101", Department="03_Grocery 2", item_desc_eng="JUICE", **{
        "Standardize Unit Size": 500.5,
        "Standardize UOM": "ml",
        "Standardize Pack Size": 1,
    })
    add(Item_no="000102", Department="03_Grocery 2", item_desc_eng="JUICE", **{
        "Standardize Unit Size": 0,
        "Standardize UOM": "ML",
        "Standardize Pack Size": 1,
    })
    add(Item_no="000103", Department="03_Grocery 2", item_desc_eng="JUICE", **{
        "Standardize Unit Size": "500.5",
        "Standardize UOM": "ML",
        "Standardize Pack Size": 1,
    })
    fixture = tmp_path / "group-a-gate.xlsx"
    workbook.save(fixture)

    storage = LocalFileStorage(tmp_path / "files", 10_000_000)
    job_id = "aabbccdd"
    with fixture.open("rb") as source:
        storage.save_input(job_id, source)
    repository = FakeRepositories({
        "job_id": job_id,
        "selected_departments": ["03_Grocery 2", "06_Dairy & Frozen"],
        "snapshot_label": None,
    })

    JobProcessor(repository, storage, load_default_registry(), MockInferenceProvider()).process(job_id)

    by_item = {item["item_no"]: item for item in repository.items}
    normalized = by_item["000101"]
    assert normalized["group"] == "B"
    assert normalized["reason_code"] == "STANDARD_FIELDS_NORMALIZATION"
    assert normalized["field_proposals"] == {
        "standard_size": None,
        "standard_uom": "ML",
        "standard_pack_size": None,
    }
    assert normalized["rule"]["rounding_decimals"] is None
    assert normalized["original"]["standard_uom"] == "ml"
    assert normalized["validation"]["status"] == "AUTO_FIX"

    numeric_text = by_item["000103"]
    assert numeric_text["group"] == "B"
    assert numeric_text["field_proposals"] == {
        "standard_size": "500.5",
        "standard_uom": None,
        "standard_pack_size": None,
    }
    assert numeric_text["rule"]["rounding_decimals"] is None

    invalid = by_item["000102"]
    assert invalid["group"] == "DATA_SHAPE_ERROR"
    assert invalid["reason_code"] == "GROUP_A_VALIDATION_INVALID"
    assert invalid["validation"]["status"] == "INVALID"


class PackAwareProvider:
    def __init__(self):
        self.requests = []

    async def infer(self, request):
        self.requests.append(request)
        if request.task == "PACK_ONLY":
            return InferenceResponse(
                result=InferenceResult(
                    status="PACK_PROPOSAL",
                    pack_size=4,
                    pack_evidence=Evidence(field="item_desc_eng", fragment="4'S"),
                    confidence="HIGH",
                    reason_code="EXPLICIT_PACK_COUNT",
                ),
                metadata=ProviderMetadata(
                    provider="mock", agent_name="pack-test", agent_version="1"
                ),
            )
        return InferenceResponse(
            result=InferenceResult(
                status="PROPOSAL",
                measurement=ObservedMeasurement(
                    value=500,
                    uom="ML",
                    field="item_desc_eng",
                    fragment="500ML",
                ),
                confidence="HIGH",
                reason_code="EXPLICIT_MEASUREMENT",
            ),
            metadata=ProviderMetadata(
                provider="mock", agent_name="measurement-test", agent_version="1"
            ),
        )


def test_b_and_c_share_evidence_first_pack_pipeline(tmp_path: Path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = INPUT_SHEET
    headers = sorted(set(FIELD_MAP.values()) | PURGE_HEADERS)
    sheet.append(headers)

    def add(**values):
        sheet.append([values.get(header) for header in headers])

    add(Item_no="000201", Department="03_Grocery 2", item_desc_eng="JUICE 6 X 500ML",
        item_size_value=500, item_size_unit="ML")
    add(Item_no="000202", Department="03_Grocery 2", item_desc_eng="JUICE ASSORTED 4'S 500ML",
        item_size_value=500, item_size_unit="ML")
    add(Item_no="000203", Department="06_Dairy & Frozen", item_desc_eng="WATER 6 X 500ML")
    fixture = tmp_path / "pack-pipeline.xlsx"
    workbook.save(fixture)

    storage = LocalFileStorage(tmp_path / "files", 10_000_000)
    job_id = "facefeed"
    with fixture.open("rb") as source:
        storage.save_input(job_id, source)
    repository = FakeRepositories({
        "job_id": job_id,
        "selected_departments": ["03_Grocery 2", "06_Dairy & Frozen"],
        "snapshot_label": None,
    })
    provider = PackAwareProvider()

    JobProcessor(repository, storage, load_default_registry(), provider).process(job_id)

    by_item = {item["item_no"]: item for item in repository.items}
    deterministic_b = by_item["000201"]
    assert deterministic_b["field_proposals"]["standard_pack_size"] == "6"
    assert deterministic_b["pack_result"]["status"] == "DETERMINISTIC_PROPOSAL"
    assert deterministic_b["field_provenance"]["standard_pack_size"]["method"] == "RULE"

    agent_b = by_item["000202"]
    assert agent_b["field_proposals"]["standard_pack_size"] == "4"
    assert agent_b["pack_result"]["status"] == "AGENT_PROPOSAL"
    assert agent_b["field_provenance"]["standard_pack_size"]["method"] == "AI_INFERENCE"
    assert agent_b["field_provenance"]["standard_size"]["method"] == "RULE"

    deterministic_c = by_item["000203"]
    assert deterministic_c["field_proposals"] == {
        "standard_size": "500",
        "standard_uom": "ML",
        "standard_pack_size": "6",
    }
    assert deterministic_c["pack_result"]["status"] == "DETERMINISTIC_PROPOSAL"

    assert [request.task for request in provider.requests].count("PACK_ONLY") == 1
    assert [request.task for request in provider.requests].count("MEASUREMENT_AND_PACK") == 1


def _run_single_b_pack_case(tmp_path: Path, provider, *, enabled: bool):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = INPUT_SHEET
    headers = sorted(set(FIELD_MAP.values()) | PURGE_HEADERS)
    sheet.append(headers)
    values = {
        "Item_no": "000301",
        "Department": "03_Grocery 2",
        "item_desc_eng": "JUICE ASSORTED 4'S 500ML",
        "item_size_value": 500,
        "item_size_unit": "ML",
    }
    sheet.append([values.get(header) for header in headers])
    fixture = tmp_path / "single-pack.xlsx"
    workbook.save(fixture)
    storage = LocalFileStorage(tmp_path / "files", 10_000_000)
    job_id = "deadbeef"
    with fixture.open("rb") as source:
        storage.save_input(job_id, source)
    repository = FakeRepositories({
        "job_id": job_id,
        "selected_departments": ["03_Grocery 2", "06_Dairy & Frozen"],
        "snapshot_label": None,
    })
    JobProcessor(
        repository, storage, load_default_registry(), provider,
        pack_size_inference_enabled=enabled,
    ).process(job_id)
    return repository.items[0]


class MustNotCallProvider:
    async def infer(self, request):
        raise AssertionError("provider must not be called")


def test_pack_agent_feature_flag_does_not_affect_b_conversion(tmp_path: Path):
    item = _run_single_b_pack_case(tmp_path, MustNotCallProvider(), enabled=False)
    assert item["field_proposals"]["standard_size"] == "500"
    assert item["field_proposals"]["standard_uom"] == "ML"
    assert item["field_proposals"]["standard_pack_size"] is None
    assert item["pack_result"]["status"] == "AGENT_DISABLED"


class FailingPackProvider:
    async def infer(self, request):
        raise RuntimeError("provider unavailable")


def test_pack_agent_failure_isolated_from_b_conversion(tmp_path: Path):
    item = _run_single_b_pack_case(tmp_path, FailingPackProvider(), enabled=True)
    assert item["field_proposals"]["standard_size"] == "500"
    assert item["field_proposals"]["standard_uom"] == "ML"
    assert item["field_proposals"]["standard_pack_size"] is None
    assert item["pack_result"]["status"] == "AGENT_ERROR"
    assert item["pack_result"]["reason_code"] == "PACK_AGENT_ERROR"
