"""A job whose process died mid-run must become a failure the user can restart."""
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import jobs
from app.api.dependencies import processor, repositories


class FakeRepositories:
    def __init__(self, status, age_minutes):
        self.job = {"job_id": "j1", "status": status, "progress": {"stage": status, "percent": 50},
                    "updated_at": datetime.now(timezone.utc) - timedelta(minutes=age_minutes)}
        self.updates = []
        self.deleted = 0

    def get_job(self, job_id):
        return dict(self.job) if job_id == "j1" else None

    def update_job(self, job_id, values):
        self.updates.append(values)
        for key, value in values.items():
            if "." not in key:
                self.job[key] = value

    def delete_items(self, job_id):
        self.deleted += 1
        return 12000

    headroom = (417.0, 512.0)

    def storage_headroom(self):
        return self.headroom


class FakeProcessor:
    class registry:
        version = "poc-v3"
        checksum = "abc"

    def __init__(self):
        self.started = []

    def process(self, job_id):
        self.started.append(job_id)


def client(repos, service=None):
    app = FastAPI()
    app.include_router(jobs.router, prefix="/api")
    app.dependency_overrides[repositories] = lambda: repos
    app.dependency_overrides[processor] = lambda: service or FakeProcessor()
    return TestClient(app)


def test_a_job_silent_for_too_long_is_reported_as_interrupted_and_failed():
    repos = FakeRepositories("PROCESSING", age_minutes=17)
    body = client(repos).get("/api/jobs/j1").json()
    assert body["status"] == "FAILED"
    assert "interrupted" in body["error"] and "Process again" in body["error"]
    assert repos.job["status"] == "FAILED"


def test_a_job_still_reporting_progress_is_left_alone():
    repos = FakeRepositories("PROCESSING", age_minutes=2)
    assert client(repos).get("/api/jobs/j1").json()["status"] == "PROCESSING"
    assert repos.updates == []


def test_finished_jobs_are_never_marked_interrupted():
    repos = FakeRepositories("EXPORTED", age_minutes=600)
    assert client(repos).get("/api/jobs/j1").json()["status"] == "EXPORTED"


def test_restarting_an_interrupted_job_clears_its_partial_rows_and_runs_again():
    repos = FakeRepositories("PROCESSING", age_minutes=17)
    service = FakeProcessor()
    response = client(repos, service).post("/api/jobs/j1/process")
    assert response.status_code == 202
    assert repos.deleted == 1
    assert service.started == ["j1"]
    assert repos.job["status"] == "VALIDATING"


def test_a_live_job_cannot_be_restarted_underneath_itself():
    repos = FakeRepositories("PROCESSING", age_minutes=1)
    assert client(repos).post("/api/jobs/j1/process").status_code == 409
    assert repos.deleted == 0


def test_processing_is_refused_when_the_database_has_no_room():
    repos = FakeRepositories("UPLOADED", age_minutes=0)
    repos.headroom = (490.0, 512.0)
    response = client(repos).post("/api/jobs/j1/process")
    assert response.status_code == 507
    assert "490 MB of 512 MB" in response.json()["detail"]
    assert repos.deleted == 0
