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
