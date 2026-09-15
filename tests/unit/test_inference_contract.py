import pytest
from pydantic import ValidationError

from app.agents.provider import InferenceRequest, InferenceResult, ObservedMeasurement, validate_evidence


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
