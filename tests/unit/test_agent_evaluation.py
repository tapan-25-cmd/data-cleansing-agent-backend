import re

from app.agents.provider import (
    Evidence, InferenceResponse, InferenceResult, ObservedMeasurement, ProviderMetadata,
)
from app.rules.registry import load_default_registry
from app.services.agent_evaluation_service import AgentEvaluationService, load_cases

MEASURE = re.compile(r"(\d+(?:\.\d+)?)\s*(KG|GM|G|ML|OZ|L|克|毫升|公升|安士)(?![A-WYZ])", re.IGNORECASE)
METADATA = ProviderMetadata(provider="google-adk", agent_name="a", agent_version="2.0.0",
                            prompt_version="uom-inference-v3", model_id="m", input_tokens=10, output_tokens=5)


def first_measurement(request):
    for field in ("item_desc_eng", "item_desc_local_lang", "web_description_eng", "web_description_chi"):
        match = MEASURE.search(getattr(request, field) or "")
        if match:
            return field, match
    return None, None


class NaiveProvider:
    """Behaves like the v2 agent: the first number with a unit is the product size."""

    async def infer(self, request):
        field, match = first_measurement(request)
        if not match:
            result = InferenceResult(status="NOT_IN_DESCRIPTION")
        else:
            result = InferenceResult(status="PROPOSAL", measurement=ObservedMeasurement(
                value=match.group(1), uom=match.group(2), field=field, fragment=match.group(0),
            ))
        return InferenceResponse(result=result, metadata=METADATA)


def report_for(provider):
    return AgentEvaluationService(provider, load_default_registry()).run()


def test_case_file_is_valid_and_covers_every_audit_pattern():
    version, cases = load_cases()
    assert version == "agent-eval-v1" and len(cases) >= 28
    assert {case["pattern"] for case in cases} == {
        "capacity", "name_or_grade", "piece_vs_total", "bundle", "contents_or_case",
        "decline", "local_language", "plain",
    }
    for case in cases:
        assert set(case["text"]) <= {
            "item_brand_eng", "item_brand_local_lang", "item_desc_eng", "item_desc_local_lang",
            "web_description_eng", "web_description_chi",
        }
        size = case["expect"]["size"]
        assert size == "DECLINE" or re.fullmatch(r"\d+(\.\d+)? (GM|ML|EA|FT)", size), case["id"]


def test_the_gate_catches_the_v2_failure_patterns():
    report = report_for(NaiveProvider())
    by_pattern = {row["pattern"]: row for row in report["patterns"]}
    # A reader that takes the first number is confidently wrong on exactly the audit's patterns...
    assert by_pattern["capacity"]["wrong_and_confident"] >= 3
    assert by_pattern["name_or_grade"]["wrong_and_confident"] >= 3
    # ...and right on the plain ones, which is why a single accuracy figure hid the problem.
    assert by_pattern["plain"]["passed"] == by_pattern["plain"]["cases"]
    assert report["wrong_and_confident"] > 0 and report["passed"] < report["cases"]
    microwave = next(row for row in report["rows"] if row["id"] == "capacity_microwave_box")
    assert (microwave["answer"], microwave["expected"], microwave["wrong_and_confident"]) == (
        "1000 ML", "No size applied", True,
    )


class RoleAwareProvider(NaiveProvider):
    """The same naive reading, but honest about roles: the backend gate does the rest."""

    async def infer(self, request):
        response = await super().infer(request)
        text = f"{request.item_desc_eng} {request.item_desc_local_lang}"
        if response.result.measurement and any(word in text for word in ("BOX", "SCALE", "CUP", "RING")):
            response.result.measurement.role = "CAPACITY_OR_RANGE"
        return response


def test_a_non_size_role_is_refused_by_the_pipeline_not_by_trusting_the_model():
    report = report_for(RoleAwareProvider())
    capacity = next(row for row in report["patterns"] if row["pattern"] == "capacity")
    assert (capacity["passed"], capacity["wrong_and_confident"]) == (capacity["cases"], 0)


class FailingProvider:
    async def infer(self, request):
        raise RuntimeError("unavailable")


def test_failed_calls_are_reported_and_never_scored_as_correct():
    report = report_for(FailingProvider())
    assert report["failed_calls"] == report["cases"] and report["passed"] == 0
    assert report["wrong_and_confident"] == 0


def test_contents_pack_is_not_applied():
    class ContentsProvider:
        async def infer(self, request):
            return InferenceResponse(metadata=METADATA, result=InferenceResult(
                status="PROPOSAL", pack_size="16", pack_role="CONTENTS",
                pack_evidence=Evidence(field="web_description_eng", fragment="16PK"),
                measurement=ObservedMeasurement(value="480", uom="G", field="web_description_eng",
                                                fragment="480G", role="NET_CONTENT_TOTAL"),
            )) if "16PK" in (request.web_description_eng or "") else InferenceResponse(
                metadata=METADATA, result=InferenceResult(status="NOT_IN_DESCRIPTION"))

    row = next(r for r in report_for(ContentsProvider())["rows"] if r["id"] == "contents_with_total")
    assert (row["answer"], row["passed"]) == ("480 GM", True)
