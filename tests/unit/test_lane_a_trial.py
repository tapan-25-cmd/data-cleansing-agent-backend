"""The reconcile task's contract and scoring, without any model call."""
from decimal import Decimal

import pytest

from app.agents.reconcile import ReconcileRequest, ReconcileResult, ReconcileResponse, SourceValue, RuleOutcome, validate_evidence
from app.services.lane_a_trial_service import build_request, category_kind, load_reconcile_cases, score_row


def item(**changes):
    base = {
        "row_number": 17360, "item_no": "550624", "route": "A",
        "context": {"item_desc_eng": "TY SHRIMP CR NDL\\10", "item_desc_local_lang": "冬蔭蝦味奶油湯麵", "web_description_eng": "TY SHRIMP CR NDL\\10"},
        "original": {"legacy_size": "55", "legacy_uom": "GM", "standard_size": 550, "standard_uom": "GM", "standard_pack_size": 1},
        "field_proposals": {"standard_size": "55", "standard_uom": "GM", "standard_pack_size": "10"},
        "application_policy": "REVIEW_REQUIRED", "reason_code": "VALIDATED_BASE_UNIT",
        "review": {"overall_status": "PENDING"}, "changes": [],
        "findings": [{"code": "LINKED_SIZE_AND_PACK_SUGGESTION", "severity": "REVIEW", "field": "standard_size",
                      "evidence": [{"role": "CURRENT", "value": "550 GM × 1"}, {"role": "EXPECTED", "value": "\\10"}],
                      "proposed": {"standard_size": "55", "standard_uom": "GM", "standard_pack_size": "10"}}],
    }
    base.update(changes)
    return base


def response(**changes) -> ReconcileResponse:
    result = {
        "product_unit": "pack of instant noodles", "verdict": "COMBINED",
        "proposed": {"standard_size": "55", "standard_uom": "gm", "standard_pack_size": "10"},
        "explanation": "Ten packs of 55 GM.", "evidence": [{"field": "item_desc_eng", "fragment": "\\10"}],
        "needs_business_rule": True, "confidence": "HIGH",
    }
    result.update(changes)
    return ReconcileResponse(result=ReconcileResult.model_validate(result), model_id="m", prompt_version="uom-reconcile-v1",
                             prompt_sha256="x", latency_ms=1, input_tokens=10, output_tokens=5, attempts=1)


def test_request_carries_every_source_and_the_rule_outcome_without_columns_b_and_c():
    request = build_request(item())
    assert set(request.descriptions) == {"item_brand_eng", "item_brand_local_lang", "item_desc_eng", "item_desc_local_lang", "web_description_eng", "web_description_chi"}
    assert request.legacy == SourceValue(size="55", uom="GM")
    assert request.excel.total == "550"
    assert request.rules.label == "Needs your review" and request.rules.suggestion.pack_size == "10"


def test_cannot_tell_drops_any_proposal_and_other_verdicts_need_one():
    assert ReconcileResult.model_validate({"product_unit": "x", "verdict": "CANNOT_TELL", "explanation": "silent",
                                           "proposed": {"standard_size": "1", "standard_uom": "GM"}}).proposed is None
    with pytest.raises(ValueError):
        ReconcileResult.model_validate({"product_unit": "x", "verdict": "EXCEL_RIGHT", "explanation": "ok"})


def test_evidence_must_be_quoted_from_a_description_field():
    request = build_request(item())
    validate_evidence(request, response().result)
    with pytest.raises(ValueError):
        validate_evidence(request, response(evidence=[{"field": "item_desc_eng", "fragment": "not there"}]).result)
    with pytest.raises(ValueError):
        validate_evidence(request, response(evidence=[{"field": "legacy", "fragment": "55"}]).result)


def test_scoring_against_the_engine_and_the_reviewer():
    cases = {"550624": {"expected": {"standard_size": "55", "standard_uom": "GM", "standard_pack_size": "10", "total": "550"}, "answer": "55 GM × 10"}}
    row = score_row(item(), response(), cases)
    assert row["agreement"] == "AGREES_WITH_SUGGESTION"
    assert row["reviewer"]["score"] == "MATCHES_REVIEWER"
    assert row["ai"]["values"]["standard_uom"] == "GM" and row["ai"]["values"]["total"] == "550"

    kept = score_row(item(), response(verdict="EXCEL_RIGHT", proposed={"standard_size": "550", "standard_uom": "GM", "standard_pack_size": "1"}), cases)
    assert kept["agreement"] == "KEEPS_EXCEL_AGAINST_SUGGESTION" and kept["reviewer"]["score"] == "DIFFERS_FROM_REVIEWER"

    silent = score_row(item(), response(verdict="CANNOT_TELL", proposed=None), cases)
    assert silent["agreement"] == "CANNOT_TELL" and silent["reviewer"]["score"] == "CANNOT_TELL"
    assert silent["ai"]["values"] is None


def test_proposed_quantities_must_be_positive():
    with pytest.raises(ValueError):
        response(proposed={"standard_size": "-1", "standard_uom": "GM", "standard_pack_size": "1"})
    assert response(proposed={"standard_size": Decimal("55"), "standard_uom": "gm"}).result.proposed.standard_uom == "GM"


def test_category_kind_comes_from_the_job_profile():
    profile = {"liquid_categories": ["Juices"], "mixed_categories": ["Sauces"]}
    assert category_kind(item(context={"category": "Juices"}), profile) == "LIQUID"
    assert category_kind(item(context={"category": "Sauces"}), profile) == "MIXED"
    assert category_kind(item(context={"category": "Rice"}), profile) == "UNKNOWN"
    assert build_request(item(context={"category": "Juices", "item_desc_eng": "X"}), profile).category_kind == "LIQUID"


def test_case_file_scores_expected_verdicts_and_flags_apply_candidates():
    version, cases = load_reconcile_cases()
    assert version == "reconcile-eval-v1" and cases["374470"]["verdict"] == "CANNOT_TELL"
    row = score_row(item(), response(), {})
    assert row["expected"]["score"] == "MATCHES_EXPECTED"
    assert row["tier"] == "SUGGEST"  # needs_business_rule keeps it a suggestion
    clean = score_row(item(), response(needs_business_rule=False), {})
    assert clean["tier"] == "APPLY_CANDIDATE" and clean["guards"] == []
    ounce = score_row(item(), response(proposed={"standard_size": "16", "standard_uom": "OZ", "standard_pack_size": "1"}, verdict="EXCEL_RIGHT", needs_business_rule=False), {})
    assert "NON_STANDARD_UNIT" in ounce["guards"] and ounce["tier"] == "SUGGEST"
