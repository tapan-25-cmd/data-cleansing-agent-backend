from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import reasoning
from app.api.dependencies import repositories


class FakeRepositories:
    def __init__(self):
        self.trial = {"job_id": "j1", "run_id": "r1", "summary": {"rows": 1, "tiers": {"SUGGEST": 1}}, "by_category": {"same_total_different_split": {"SUGGEST": 1}}}
        self.rows = [{"job_id": "j1", "run_id": "r1", "row_number": 3, "ai": {"verdict": "COMBINED"}}]

    def get_job(self, job_id):
        return {"job_id": "j1"} if job_id == "j1" else None

    def latest_lane_a_trial(self, job_id):
        return self.trial

    def lane_a_trial_row(self, job_id, row_number):
        return next((r for r in self.rows if r["row_number"] == row_number), None)


def client(repos):
    app = FastAPI()
    app.include_router(reasoning.router, prefix="/api")
    app.dependency_overrides[repositories] = lambda: repos
    return TestClient(app)


def test_summary_and_row_are_served_from_the_latest_trial():
    api = client(FakeRepositories())
    assert api.get("/api/jobs/j1/reasoning").json()["summary"]["tiers"] == {"SUGGEST": 1}
    assert api.get("/api/jobs/j1/reasoning/3").json()["row"]["ai"]["verdict"] == "COMBINED"
    assert api.get("/api/jobs/j1/reasoning/4").json() == {"status": "NOT_RUN", "row": None}
    assert api.get("/api/jobs/nope/reasoning").status_code == 404
