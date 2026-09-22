from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import evaluations
from app.rules.registry import load_default_registry
from app.services.agent_evaluation_service import AgentEvaluationService
from tests.unit.test_agent_evaluation import NaiveProvider


class Repos:
    def __init__(self):
        self.saved = []

    def save_agent_evaluation(self, document):
        self.saved.append(document)

    def agent_evaluations(self, limit=10):
        return list(reversed(self.saved))[:limit]


def client(real=True):
    app = FastAPI()
    app.include_router(evaluations.router, prefix="/api")
    app.state.repositories = Repos()
    app.state.agent_evaluation = AgentEvaluationService(NaiveProvider(), load_default_registry())
    app.state.ai_reading_test = type("T", (), {"provider_is_real": real})()
    return TestClient(app), app


def test_hard_case_run_is_stored_and_listed_newest_first():
    api, app = client()
    assert api.get("/api/evaluations/agent").json()["runs"] == []
    assert api.post("/api/evaluations/agent/run").status_code == 202  # background task runs inline here

    listing = api.get("/api/evaluations/agent").json()
    assert listing["running"] is False and listing["cases"] >= 28
    run = listing["runs"][0]
    assert run["cases"] == listing["cases"] and run["wrong_and_confident"] > 0
    assert {"pattern", "cases", "passed", "wrong_and_confident"} <= set(run["patterns"][0])


def test_a_paid_run_is_refused_when_the_ai_is_switched_off():
    api, _ = client(real=False)
    response = api.post("/api/evaluations/agent/run")
    assert response.status_code == 409 and "switched off" in response.json()["detail"]
