from app.services.evaluation_service import load_evaluation_catalog


def test_catalog_is_versioned_and_does_not_claim_unapproved_accuracy():
    catalog = load_evaluation_catalog()

    assert catalog["version"] == "uom-golden-v1"
    assert catalog["summary"]["total_cases"] >= 5
    assert catalog["summary"]["approved_labels"] >= 2
    assert catalog["summary"]["accuracy_available"] is True
    assert catalog["summary"]["approved_passed"] == catalog["summary"]["approved_labels"]
    assert catalog["summary"]["approved_accuracy_percent"] == 100.0
    assert catalog["summary"]["coverage"]["deterministic_cases"] == catalog["summary"]["total_cases"]
    assert catalog["summary"]["coverage"]["adk_agent_score_available"] is False
    assert len(catalog["summary"]["business_decisions"]) == 3
    assert all("actual" in case and "passed" in case for case in catalog["cases"])
    assert all(case["label_status"] in {"APPROVED", "PROPOSED"} for case in catalog["cases"])
    assert all(case.get("plain_language", {}).get("question") for case in catalog["cases"])
