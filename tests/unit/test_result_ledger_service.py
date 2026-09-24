from app.services.result_ledger_service import changes_after_review, enrich_result_item


def item(*, issues=None, proposals=None):
    return {
        "route": "A",
        "original": {
            "standard_size": "375",
            "standard_uom": "GM",
            "standard_pack_size": "1",
        },
        "field_proposals": proposals or {
            "standard_size": None,
            "standard_uom": None,
            "standard_pack_size": None,
        },
        "field_provenance": {},
        "method": "NONE",
        "rule": {},
        "validation": {"issues": issues or []},
        "review": {
            "field_decisions": {},
            "overall_status": "NOT_REQUIRED",
            "override_values": None,
            "comment": None,
        },
    }


def test_material_mismatch_becomes_review_required_change_without_overwrite():
    result = enrich_result_item(item(issues=[{
        "code": "SIGNIFICANT_LEGACY_SIZE_MISMATCH",
        "field": "standard_size",
        "severity": "WARNING",
        "message": "Existing differs from conversion",
        "current_value": "375",
        "expected_value": "349",
    }]))

    assert result["application_policy"] == "REVIEW_REQUIRED"
    assert result["field_proposals"]["standard_size"] == "349"
    change = next(row for row in result["changes"] if row["field"] == "standard_size")
    assert change == {
        "field": "standard_size",
        "original": "375",
        "proposed": "349",
        "final": "375",
        "action": "REVIEW_REQUIRED",
        "method": "RULE",
        "rule_id": "LEGACY_COMPARISON",
        "confidence": None,
    }


def test_rounding_variance_is_observation_only():
    result = enrich_result_item(item(issues=[{
        "code": "ROUNDING_ONLY_VARIANCE",
        "field": "standard_size",
        "severity": "INFO",
        "message": "Within tolerance",
        "current_value": "484",
        "expected_value": "485",
    }]))

    assert result["application_policy"] == "OBSERVATION_ONLY"
    assert result["field_proposals"]["standard_size"] is None
    assert result["changes"][0]["final"] == "375"


def test_legacy_total_consistency_never_creates_a_k_only_proposal():
    result = enrich_result_item(item(issues=[{
        "code": "LEGACY_TOTAL_CONSISTENT",
        "field": "standard_size",
        "severity": "INFO",
        "message": "Legacy matches K times M",
        "current_value": "70 GM × 5",
        "expected_value": "350 GM",
    }]))

    assert result["application_policy"] == "OBSERVATION_ONLY"
    assert result["field_proposals"]["standard_size"] is None
    assert result["changes"][0]["final"] == "375"


def test_existing_rule_proposal_is_auto_apply():
    result = enrich_result_item(item(proposals={
        "standard_size": "1000",
        "standard_uom": "GM",
        "standard_pack_size": None,
    }))

    assert result["application_policy"] == "AUTO_APPLY"
    assert result["changes"][0]["final"] == "1000"


def test_review_approval_and_rejection_refresh_final_values():
    pending = enrich_result_item(item(issues=[{
        "code": "SIGNIFICANT_LEGACY_SIZE_MISMATCH",
        "field": "standard_size",
        "severity": "WARNING",
        "message": "Mismatch",
        "current_value": "375",
        "expected_value": "349",
    }]))

    approved = changes_after_review(pending, "APPROVED")
    rejected = changes_after_review(pending, "REJECTED")

    assert approved[0]["final"] == "349"
    assert approved[0]["action"] == "APPROVED"
    assert rejected[0]["final"] == "375"
    assert rejected[0]["action"] == "REJECTED"


def test_group_c_without_explicit_evidence_is_a_visible_safe_abstention():
    source = item()
    source["route"] = "C"
    source["reason_code"] = "NOT_IN_DESCRIPTION"

    result = enrich_result_item(source)

    assert result["application_policy"] == "UNRESOLVED"
    assert result["findings"][0]["code"] == "AGENT_NO_EXPLICIT_EVIDENCE"
    assert "abstained instead of guessing" in result["findings"][0]["human_reason"]


def test_different_legacy_and_standardized_dimensions_require_human_review():
    source = item(issues=[{
        "code": "LEGACY_UOM_MISMATCH",
        "field": "standard_uom",
        "severity": "WARNING",
        "message": "Legacy conversion targets a different standardized UOM",
        "current_value": "GM",
        "expected_value": "ML",
    }])
    source["original"]["legacy_size"] = "100"
    source["original"]["legacy_uom"] = "ML"

    result = enrich_result_item(source)

    assert result["application_policy"] == "REVIEW_REQUIRED"
    assert result["review"]["overall_status"] == "PENDING"
    assert all(value is None for value in result["field_proposals"].values())


def discrepancy(status, classification, *, aspect="COUNT", pair="item_desc"):
    return {"flagged": status == "CONFLICT", "details": [{
        "scope": "BILINGUAL_PAIR", "pair": pair, "aspect": aspect,
        "status": status, "classification": classification,
        "left_signals": [{"field": "item_desc_eng", "fragment": "9S", "value": 9}],
        "right_signals": [{"field": "item_desc_local_lang", "fragment": "3支", "value": 3}],
    }]}


def test_description_conflict_blocks_auto_apply_until_reviewed():
    source = item(proposals={
        "standard_size": "1000", "standard_uom": "GM", "standard_pack_size": None,
    })
    source["route"] = "B"
    source["discrepancy"] = discrepancy("CONFLICT", "COUNT_CONFLICT")

    result = enrich_result_item(source)

    assert result["application_policy"] == "REVIEW_REQUIRED"
    assert result["review"]["overall_status"] == "PENDING"
    finding = result["findings"][0]
    assert finding["code"] == "PACK_COUNT_CONFLICT"
    assert finding["severity"] == "REVIEW"
    assert "“9S”" in finding["human_reason"] and "“3支”" in finding["human_reason"]
    assert [e["label"] for e in finding["evidence"]] == [
        "Item description (English)", "Item description (local language)",
    ]
    # The rule proposal is kept for the reviewer but is not the final value.
    size = next(c for c in result["changes"] if c["field"] == "standard_size")
    assert (size["proposed"], size["final"], size["action"]) == ("1000", "375", "REVIEW_REQUIRED")


def test_packaging_level_observation_never_requires_review():
    source = item()
    source["discrepancy"] = discrepancy(
        "OBSERVATION", "PACKAGING_LEVEL_DIFFERENCE", aspect="MEASUREMENT",
    )
    result = enrich_result_item(source)
    assert result["application_policy"] == "OBSERVATION_ONLY"
    assert result["findings"][0]["severity"] == "INFO"


def test_missing_side_is_not_a_finding():
    source = item()
    source["discrepancy"] = discrepancy(
        "INSUFFICIENT", "INSUFFICIENT_COMPARISON_DATA", aspect="MEASUREMENT",
    )
    result = enrich_result_item(source)
    assert result["findings"] == []
    assert result["application_policy"] == "NO_CHANGE"


def test_bilingual_measurement_conflict_is_not_reported_twice():
    source = item(issues=[{
        "code": "BILINGUAL_DESCRIPTION_CONFLICT", "field": "item_desc",
        "severity": "ERROR", "message": "conflict",
    }])
    source["discrepancy"] = discrepancy("CONFLICT", "VALUE_CONFLICT", aspect="MEASUREMENT")
    result = enrich_result_item(source)
    assert [f["code"] for f in result["findings"]] == ["BILINGUAL_DESCRIPTION_CONFLICT"]


def test_linked_suggestion_proposes_size_and_pack_together_for_review():
    result = enrich_result_item(item(issues=[{
        "code": "LINKED_SIZE_AND_PACK_SUGGESTION",
        "field": "standard_size",
        "severity": "WARNING",
        "message": "55 × 10 = 550",
        "current_value": "550 GM × 1",
        "expected_value": "\\10 (item description, English)",
        "proposed": {"standard_size": "55", "standard_uom": "GM", "standard_pack_size": "10"},
    }]))

    assert result["application_policy"] == "REVIEW_REQUIRED"
    assert result["field_proposals"] == {"standard_size": "55", "standard_uom": "GM", "standard_pack_size": "10"}
    assert result["field_provenance"]["standard_pack_size"]["rule_id"] == "LEGACY_LINKED_PACK"
    finding = next(f for f in result["findings"] if f["code"] == "LINKED_SIZE_AND_PACK_SUGGESTION")
    assert finding["proposed"]["standard_pack_size"] == "10"


def test_a_converted_single_item_gets_pack_size_one_with_a_note():
    source = item(proposals={"standard_size": "1000", "standard_uom": "GM", "standard_pack_size": None})
    source.update({"route": "B", "reason_code": "RULE_CONVERSION",
                   "original": {"standard_size": None, "standard_uom": None, "standard_pack_size": None, "legacy_size": "1", "legacy_uom": "KG"},
                   "pack_result": {"status": "NOT_FOUND", "candidates": []}})
    result = enrich_result_item(source)
    assert result["field_proposals"]["standard_pack_size"] == "1"
    assert result["field_provenance"]["standard_pack_size"]["rule_id"] == "SINGLE_ITEM_DEFAULT"
    assert any(f["code"] == "PACK_SIZE_SINGLE_ITEM" for f in result["findings"])
    assert result["application_policy"] == "AUTO_APPLY"


def test_no_single_item_default_when_a_count_was_seen_or_a_review_is_open():
    seen = item(proposals={"standard_size": "1000", "standard_uom": "GM", "standard_pack_size": None})
    seen.update({"route": "B", "reason_code": "RULE_CONVERSION",
                 "original": {"standard_size": None, "standard_uom": None, "standard_pack_size": None, "legacy_size": "1", "legacy_uom": "KG"},
                 "pack_result": {"status": "AGENT_DECLINED", "candidates": [{"pack_size": "6"}]}})
    assert enrich_result_item(seen)["field_proposals"]["standard_pack_size"] is None
