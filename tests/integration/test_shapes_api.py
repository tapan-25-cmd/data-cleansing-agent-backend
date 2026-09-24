from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import shapes


def test_every_shape_has_a_group_and_the_rules_match_the_classifier():
    app = FastAPI(); app.include_router(shapes.router, prefix="/api")
    body = TestClient(app).get("/api/rules/shapes").json()
    assert len(body["rows"]) == 32
    by = {tuple(r["has"][k] for k in "IJKLM"): r for r in body["rows"]}
    assert by[(True, True, True, True, True)]["group"] == "A"
    assert by[(True, True, False, False, False)]["group"] == "B" and by[(True, True, False, False, False)]["outcome"] == "Filled in"
    assert by[(False, True, False, False, False)]["outcome"] == "Left empty"
    assert by[(False, False, False, False, False)]["group"] == "C"
    assert by[(True, False, False, False, False)]["group"] == "C"
    assert by[(True, True, True, True, False)]["group"] == "INVALID"
    assert set(body["groups"]) == {"A", "B", "C", "INVALID"}
