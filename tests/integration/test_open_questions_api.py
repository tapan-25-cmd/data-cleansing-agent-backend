from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import open_questions, rules
from app.api.dependencies import repositories
from app.rules.registry import load_default_registry


class FakeRepositories:
    def __init__(self, status="READY_FOR_REVIEW"):
        self.job = {"job_id": "j1", "status": status, "original_file_name": "book.xlsx",
                    "rule_readiness": {"uncovered_source_uoms": ["ST"], "uncovered_affected_rows": 1},
                    "validation_policy": {"version": "group-a-validation-v5", "guards_version": "guards-v1",
                                          "category_profile": {"liquid_categories": ["Juices"]}}}
        self.answers = {}

    def get_job(self, job_id):
        return self.job if job_id == "j1" else None

    def attention_items(self, job_id):
        return [{
            "row_number": 14233, "item_no": "125773", "group": "A",
            "context": {"item_desc_eng": "MULTI COLOR CANDLE", "item_desc_local_lang": "彩色長蠟燭6枝"},
            "original": {"legacy_size": "1", "legacy_uom": "PK", "standard_size": 6, "standard_uom": "EA", "standard_pack_size": 1},
            "field_proposals": {"standard_size": None, "standard_uom": None, "standard_pack_size": None},
            "application_policy": "OBSERVATION_ONLY", "reason_code": "VALIDATED_BASE_UNIT",
            "review": {"overall_status": "NOT_REQUIRED"}, "changes": [],
            "findings": [{"code": "DESCRIPTION_CONFIRMS_PIECE_COUNT", "severity": "INFO", "field": "standard_size",
                          "evidence": [{"role": "CURRENT", "value": "6 EA × 1"}, {"role": "EXPECTED", "value": "6枝 (item description, local language)"}]}],
        }]

    def open_question_answers(self):
        return dict(self.answers)

    def save_open_question_answer(self, document):
        self.answers[document["category"]] = document


def client(repos):
    app = FastAPI()
    app.state.registry = load_default_registry()
    app.include_router(open_questions.router, prefix="/api")
    app.include_router(rules.router, prefix="/api")
    app.dependency_overrides[repositories] = lambda: repos
    return TestClient(app)


def test_categories_rows_and_answers():
    repos = FakeRepositories()
    api = client(repos)

    summary = api.get("/api/jobs/j1/open-questions").json()
    notes = next(g for g in summary["groups"] if g["id"] == "notes")
    confirms = next(c for c in notes["categories"] if c["id"] == "description_confirms_excel")
    assert confirms["rows"] == 1 and confirms["answer"] is None
    assert "rows" not in summary

    rows = api.get("/api/jobs/j1/open-questions/description_confirms_excel").json()
    assert rows["total"] == 1
    assert rows["rows"][0]["item_no"] == "125773"
    assert rows["rows"][0]["legacy"]["text"] == "1 PK"
    assert rows["rows"][0]["options"][0]["text"] == "6 EA × 1"
    assert api.get("/api/jobs/j1/open-questions/description_confirms_excel?search=nothing").json()["total"] == 0
    assert api.get("/api/jobs/j1/open-questions/not_a_category").status_code == 404

    recorded = api.put("/api/jobs/j1/open-questions/description_confirms_excel/answer",
                       json={"answer": "Confirmed", "note": "Candles and sachets", "answered_by": "Eric"})
    assert recorded.status_code == 200
    summary = api.get("/api/jobs/j1/open-questions").json()
    assert summary["answered"] == 1
    assert api.put("/api/jobs/j1/open-questions/nope/answer", json={"answer": "x"}).status_code == 404


def test_unfinished_or_unknown_jobs_are_rejected():
    assert client(FakeRepositories()).get("/api/jobs/nope/open-questions").status_code == 404
    assert client(FakeRepositories("PROCESSING")).get("/api/jobs/j1/open-questions").status_code == 409


def test_rules_guide_lists_flow_tables_and_versions():
    guide = client(FakeRepositories()).get("/api/rules?job_id=j1").json()
    assert [step["step"] for step in guide["flow"]] == [1, 2, 3, 4, 5, 6, 7, 8]
    kg = next(r for r in guide["unit_mapping"]["rules"] if r["rule_id"] == "KG_TO_GM")
    assert kg["example"] == "1 KG becomes 1000 GM" and "公斤" in kg["source_uoms"]
    assert guide["unit_mapping"]["readiness"]["uncovered_source_uoms"] == ["ST"]
    assert any(u["written"] == "克" and u["counted_as"] == "GM" for u in guide["description_reading"]["units"])
    assert len(guide["legacy_checks"]) == 7 and guide["guards"][0]["code"] == "OUNCE_MAY_BE_FLUID"
    assert guide["versions"]["this_job"]["validation"] == "group-a-validation-v5"
    assert guide["category_profile"]["liquid_categories"] == ["Juices"]
    nodes = {n["id"] for n in guide["pipeline"]["nodes"]}
    assert {"upload", "lane", "lane_a", "lane_b", "lane_c", "ledger", "review", "export"} <= nodes
    assert all(a in nodes and b in nodes for a, b, *_ in guide["pipeline"]["edges"])
    assert guide["reasoning"]["status"] == "SHADOW" and len(guide["reasoning"]["policy"]) == 3
    assert set(guide["accuracy_method"]) == {"A", "B", "C"} and all("{accuracy}" in "".join(v) for v in guide["accuracy_method"].values())
