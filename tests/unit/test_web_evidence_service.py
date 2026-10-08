from app.services.web_evidence_service import UNSETTLED, evaluate, verdict


def test_two_yes_answers_are_a_conflict_when_they_support_different_values():
    page = {"answer": "YES", "matched_value": "SUGGESTION"}
    photo = {"answer": "YES", "matched_value": "EXCEL"}

    outcome, _ = verdict(page, photo)
    evaluation = evaluate({
        "group": "C",
        "suggestion": {"standard_size": "200", "standard_uom": "GM", "standard_pack_size": 16},
        "final": {"standard_size": "200", "standard_uom": "GM", "standard_pack_size": 16},
        "site_text": "16 X 200 GM",
        "text": page,
        "photo": photo,
        "photo_reading": {"legible": True, "printed": "200g x 4; x16 bags", "photo": 1, "images_read": 1},
    })

    assert outcome == "SOURCE_CONFLICT"
    assert evaluation["status"] == UNSETTLED
    assert "different packaging level" in evaluation["why"]
