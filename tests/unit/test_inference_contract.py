import pytest
from pydantic import ValidationError

from app.agents.provider import Evidence, InferenceRequest, InferenceResult, ObservedMeasurement, validate_evidence


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
    with pytest.raises(ValidationError, match="requires reason code"):
        InferenceResult(
            status="PACK_PROPOSAL", pack_size=4,
            pack_evidence=Evidence(field="item_desc_eng", fragment="4 PACK"),
            confidence="HIGH", reason_code="NOT_IN_DESCRIPTION",
        )


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
