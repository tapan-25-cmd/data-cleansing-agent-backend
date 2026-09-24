import pytest
from pydantic import ValidationError

from app.agents.provider import _REASON_FOR_STATUS
from app.agents.provider import (
    Evidence,
    InferenceRequest,
    InferenceResult,
    ObservedMeasurement,
    PairInterpretation,
    QuantityRelationship,
    validate_evidence,
)


def test_inference_request_forbids_answer_fields():
    with pytest.raises(ValidationError):
        InferenceRequest(item_desc_eng="JUICE 500ML", standard_uom="ML")


def test_evidence_must_exist_verbatim():
    request = InferenceRequest(item_desc_eng="ORGANIC JUICE 500ML")
    result = InferenceResult(
        status="PROPOSAL",
        measurement=ObservedMeasurement(
            value="500", uom="ML", field="item_desc_eng", fragment="500ML"
        ),
        confidence="HIGH",
        reason_code="EXPLICIT_MEASUREMENT",
    )
    validate_evidence(request, result)
    result.measurement.fragment = "1L"
    with pytest.raises(ValueError, match="absent"):
        validate_evidence(request, result)


def test_decline_cannot_smuggle_a_proposal():
    with pytest.raises(ValidationError):
        InferenceResult(
            status="NOT_IN_DESCRIPTION",
            measurement=ObservedMeasurement(
                value="500", uom="ML", field="item_desc_eng", fragment="500ML"
            ),
            confidence="LOW",
            reason_code="BAD",
        )


def test_pack_only_proposal_requires_positive_whole_evidence_backed_count():
    request = InferenceRequest(task="PACK_ONLY", item_desc_eng="ASSORTED 4'S")
    result = InferenceResult(
        status="PACK_PROPOSAL",
        pack_size="4",
        pack_evidence=Evidence(field="item_desc_eng", fragment="4'S"),
        confidence="HIGH",
        reason_code="EXPLICIT_PACK_COUNT",
    )
    validate_evidence(request, result)


@pytest.mark.parametrize("pack_size", [0, -1, "1.5"])
def test_pack_size_must_be_a_positive_whole_number(pack_size):
    with pytest.raises(ValidationError):
        InferenceResult(
            status="PACK_PROPOSAL",
            pack_size=pack_size,
            pack_evidence=Evidence(field="item_desc_eng", fragment=f"PACK {pack_size}"),
            confidence="LOW",
            reason_code="EXPLICIT_PACK_COUNT",
        )


def test_pack_evidence_cannot_use_brand_or_omit_proposed_count():
    brand_request = InferenceRequest(task="PACK_ONLY", item_brand_eng="BRAND 6")
    brand_result = InferenceResult(
        status="PACK_PROPOSAL", pack_size=6,
        pack_evidence=Evidence(field="item_brand_eng", fragment="BRAND 6"),
        confidence="HIGH", reason_code="EXPLICIT_PACK_COUNT",
    )
    with pytest.raises(ValueError, match="description field"):
        validate_evidence(brand_request, brand_result)

    request = InferenceRequest(task="PACK_ONLY", item_desc_eng="MULTIPACK")
    result = InferenceResult(
        status="PACK_PROPOSAL", pack_size=6,
        pack_evidence=Evidence(field="item_desc_eng", fragment="MULTIPACK"),
        confidence="LOW", reason_code="EXPLICIT_PACK_COUNT",
    )
    with pytest.raises(ValueError, match="proposed count"):
        validate_evidence(request, result)

    decimal_fragment_request = InferenceRequest(
        task="PACK_ONLY", item_desc_eng="PACK 4.5"
    )
    decimal_fragment_result = InferenceResult(
        status="PACK_PROPOSAL", pack_size=4,
        pack_evidence=Evidence(field="item_desc_eng", fragment="4.5"),
        confidence="LOW", reason_code="EXPLICIT_PACK_COUNT",
    )
    with pytest.raises(ValueError, match="proposed count"):
        validate_evidence(decimal_fragment_request, decimal_fragment_result)


def test_status_and_reason_code_must_match():
    """A reason code that disagrees with the status is replaced by the one the status implies."""
    result = InferenceResult.model_validate({"status": "AMBIGUOUS", "reason_code": "NOT_IN_DESCRIPTION"})
    assert result.reason_code == _REASON_FOR_STATUS["AMBIGUOUS"]

def test_pack_only_request_rejects_measurement_response():
    request = InferenceRequest(task="PACK_ONLY", item_desc_eng="6 X 500ML")
    result = InferenceResult(
        status="PROPOSAL",
        measurement=ObservedMeasurement(
            value=500, uom="ML", field="item_desc_eng", fragment="500ML"
        ),
        confidence="HIGH",
        reason_code="EXPLICIT_MEASUREMENT",
    )
    with pytest.raises(ValueError, match="PACK_ONLY"):
        validate_evidence(request, result)


def test_pair_interpretation_can_use_one_language_when_partner_is_silent():
    request = InferenceRequest(
        item_desc_eng="BIRTHDAY CANDLES",
        item_desc_local_lang="生日蠟燭13支",
    )
    result = InferenceResult(
        status="NOT_IN_DESCRIPTION",
        pack_size=13,
        pack_evidence=Evidence(field="item_desc_local_lang", fragment="13支"),
        pack_role="CONTENTS",
        pair_interpretations=[PairInterpretation(
            pair="ITEM_DESCRIPTION",
            status="SUPPORTED",
            product_meaning="Thirteen birthday candles",
            evidence=[Evidence(field="item_desc_local_lang", fragment="13支")],
            conclusion="The local description states thirteen candles; English is silent on count.",
        )],
        quantity_relationships=[QuantityRelationship(
            relationship="CONTAINS",
            subject="birthday candles",
            count=13,
            evidence=[Evidence(field="item_desc_local_lang", fragment="13支")],
        )],
    )

    validate_evidence(request, result)


def test_relationship_requires_literal_evidence():
    with pytest.raises(ValidationError, match="literal evidence"):
        QuantityRelationship(
            relationship="UNITS_PER_PACK", subject="cakes", count=6,
        )


def test_reason_code_and_long_prose_are_normalised_instead_of_rejected():
    from app.agents.provider import InferenceResult, PairInterpretation, _REASON_FOR_STATUS
    result = InferenceResult.model_validate({
        "status": "NOT_IN_DESCRIPTION", "reason_code": _REASON_FOR_STATUS["AMBIGUOUS"],
        "pair_interpretations": [{"pair": "ITEM_DESCRIPTION", "status": "SUPPORTED", "conclusion": "x" * 900}],
    })
    assert result.reason_code == "NOT_IN_DESCRIPTION"
    assert len(result.pair_interpretations[0].conclusion) == 398
    assert isinstance(result.pair_interpretations[0], PairInterpretation)
