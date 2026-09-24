from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import quality, review
from app.api.dependencies import repositories
from app.rules.registry import load_default_registry


class FakeRepositories:
    def __init__(self, status="READY_FOR_REVIEW"):
        self.job = {"job_id": "j1", "status": status, "original_file_name": "book.xlsx",
                    "selected_departments": ["03_Grocery 2"]}
        self.item_reads = 0
        self.comparisons: dict = {}
        self.comparison_rows: dict = {}

    def get_job(self, job_id):
        return self.job if job_id == "j1" else None

    def update_job(self, job_id, values):
        self.job.update(values)

    def quality_items(self, job_id):
        self.item_reads += 1
        return [{
            "item_no": "044305", "row_number": 2, "group": "A",
            "original": {"legacy_size": "12.3", "legacy_uom": "OZ", "standard_size": "375",
                         "standard_uom": "GM", "standard_pack_size": "1"},
            "context": {"item_desc_eng": "COOKIES"}, "findings": [],
            "application_policy": "REVIEW_REQUIRED", "review": {"overall_status": "PENDING"},
        }]

    def comparison_items(self, job_id):
        past = job_id == "past"
        return [{
            "item_no": "587501", "row_number": 3, "group": "A",
            "context": {"item_desc_eng": "DRIED NOODLE"},
            "original": {
                "legacy_size": "350", "legacy_uom": "GM", "standard_size": "70",
                "standard_uom": "GM", "standard_pack_size": "5",
            },
            "field_proposals": {
                "standard_size": "350" if past else None, "standard_uom": "GM" if past else None,
                "standard_pack_size": None,
            },
            "application_policy": "REVIEW_REQUIRED" if past else "OBSERVATION_ONLY",
            "review": {"overall_status": "PENDING" if past else "NOT_REQUIRED"},
            "findings": [], "changes": [],
        }]

    def previous_completed_job(self, job):
        return {"job_id": "past", "status": "EXPORTED", "original_file_name": "v0.2.xlsx"}

    def job_comparison(self, past_job_id, new_job_id):
        return self.comparisons.get((past_job_id, new_job_id))

    def save_job_comparison(self, document):
        self.comparisons[(document["past_job_id"], document["new_job_id"])] = document

    def replace_job_comparison_rows(self, past_job_id, new_job_id, rows):
        self.comparison_rows[(past_job_id, new_job_id)] = list(rows)

    def job_comparison_rows(self, past_job_id, new_job_id, filters, page, page_size):
        from app.services.job_comparison_service import filter_rows
        rows = filter_rows(self.comparison_rows[(past_job_id, new_job_id)], **filters)
        return rows[(page - 1) * page_size:page * page_size], len(rows)

    def pending_count(self, job_id):
        return 0


def client(repos):
    app = FastAPI()
    app.state.registry = load_default_registry()
    app.include_router(quality.router, prefix="/api")
    app.dependency_overrides[repositories] = lambda: repos
    return TestClient(app)


def test_report_is_built_once_then_served_from_the_job():
    repos = FakeRepositories()
    api = client(repos)

    first = api.get("/api/jobs/j1/quality").json()
    second = api.get("/api/jobs/j1/quality").json()

    assert first == second and repos.item_reads == 1
    unit = first["capabilities"][0]
    assert (unit["tested"], unit["agreed"], unit["awaiting_decision"]) == (1, 0, 1)
    assert api.get("/api/jobs/j1/quality?refresh=true").status_code == 200
    assert repos.item_reads == 2


def test_a_review_decision_drops_the_cached_report():
    repos = FakeRepositories()
    api = client(repos)
    api.get("/api/jobs/j1/quality")
    review._refresh_job_status(repos, "j1")
    assert repos.job["quality"] is None
    api.get("/api/jobs/j1/quality")
    assert repos.item_reads == 2


def test_unknown_or_unfinished_jobs_are_rejected():
    assert client(FakeRepositories()).get("/api/jobs/nope/quality").status_code == 404
    assert client(FakeRepositories("PROCESSING")).get("/api/jobs/j1/quality").status_code == 409


def test_past_new_comparison_is_built_once_in_the_background_then_served_from_storage():
    repos = FakeRepositories()
    api = client(repos)

    first = api.get("/api/jobs/j1/comparison")
    assert first.status_code == 202
    assert first.json()["status"] == "BUILDING"
    assert first.json()["past_job"]["job_id"] == "past"

    # The test client runs the background build before returning, so it is stored now.
    assert repos.comparisons[("past", "j1")]["status"] == "READY"
    response = api.get("/api/jobs/j1/comparison")
    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "JOB_VS_JOB"
    assert payload["past_job"]["job_id"] == "past" and payload["new_job"]["job_id"] == "j1"
    assert payload["summary"]["by_change"]["REVIEW_CLEARED"] == 1
    assert payload["total"] == 1
    assert payload["rows"][0]["change_label"] == "No longer needs review"
    assert payload["rows"][0]["past"]["suggestion"]["total"] == "1750"
    assert payload["reviewer"]["new"]["ACHIEVED"] == 1

    unchanged = api.get("/api/jobs/j1/comparison?change=SAME").json()
    assert unchanged["total"] == 0
    everything = api.get("/api/jobs/j1/comparison?changed_only=false").json()
    assert everything["total"] == 1


def test_a_failed_or_stale_comparison_is_rebuilt():
    repos = FakeRepositories()
    repos.comparisons = {("past", "j1"): {
        "past_job_id": "past", "new_job_id": "j1", "status": "FAILED", "error": "boom",
    }}
    api = client(repos)
    assert api.get("/api/jobs/j1/comparison").status_code == 202
    assert repos.comparisons[("past", "j1")]["status"] == "READY"
