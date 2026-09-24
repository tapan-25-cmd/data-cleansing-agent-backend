from collections import Counter

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import shapes


def test_every_shape_has_a_method_and_the_groups_it_can_end_in():
    app = FastAPI(); app.include_router(shapes.router, prefix="/api")
    body = TestClient(app).get("/api/rules/shapes").json()
    assert len(body["rows"]) == 32
    by = {tuple(r["has"][k] for k in "IJKLM"): r for r in body["rows"]}
    complete = by[(True, True, True, True, True)]
    assert complete["route"] == "A" and complete["groups"] == ["A", "C"]
    convert = by[(True, True, False, False, False)]
    assert convert["route"] == "B" and convert["groups"] == ["B", "C"] and convert["outcome"] == "Filled in from the old size"
    unit_only = by[(False, True, False, False, False)]
    assert unit_only["groups"] == ["C"] and unit_only["outcome"] == "Could not determine"
    assert by[(False, False, False, False, False)]["route"] == "C"
    assert by[(True, False, False, False, False)]["route"] == "C"
    half = by[(True, True, True, True, False)]
    assert half["route"] == "INCOMPLETE" and half["groups"] == ["B", "C"]
    # No shape is rejected any more: every half-filled shape is completed or raised.
    assert Counter(r["route"] for r in body["rows"]) == {"A": 4, "B": 4, "C": 4, "INCOMPLETE": 20}
    assert set(body["groups"]) == {"A", "B", "C", "PURGED"}
    assert set(body["routes"]) == {"A", "B", "C", "INCOMPLETE"}
