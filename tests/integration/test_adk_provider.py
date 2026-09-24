import json

import pytest
from google.genai import types

from app.agents.adk_provider import AdkInferenceProvider
from app.agents.provider import InferenceRequest, InvalidInferenceResponseError
from app.agents.uom_inference_agent import build_uom_agent
from app.config import Settings


class FakeSessionService:
    def __init__(self):
        self.created = []
        self.deleted = []

    async def create_session(self, **values):
        self.created.append(values)

    async def delete_session(self, **values):
        self.deleted.append(values)


class FinalEvent:
    def __init__(self, payload):
        self.content = types.Content(parts=[types.Part(text=json.dumps(payload))])

    def is_final_response(self):
        return True


class FakeRunner:
    async def run_async(self, **values):
        request = json.loads(values["new_message"].parts[0].text)
        yield FinalEvent({
            "status": "PROPOSAL",
            "measurement": {
                "value": "1",
                "uom": "KG",
                "field": "item_desc_eng",
                "fragment": "1 KG",
            },
            "pack_size": None,
            "pack_evidence": None,
            "conflicting_measurements": [],
            "confidence": "HIGH",
            "reason_code": "EXPLICIT_MEASUREMENT",
        })
        assert request == {
            "task": "MEASUREMENT_AND_PACK",
            "item_desc_eng": "FLOUR 1 KG",
        }


class PackFinalRunner:
    async def run_async(self, **values):
        request = json.loads(values["new_message"].parts[0].text)
        yield FinalEvent({
            "status": "PACK_PROPOSAL",
            "measurement": None,
            "pack_size": "4",
            "pack_evidence": {"field": "item_desc_eng", "fragment": "4'S"},
            "conflicting_measurements": [],
            "confidence": "HIGH",
            "reason_code": "EXPLICIT_PACK_COUNT",
        })
        assert request["task"] == "PACK_ONLY"
        assert request["known_measurement"] == {"value": "500", "uom": "ML"}


@pytest.mark.asyncio
async def test_adk_provider_uses_fresh_sessions_and_validates_final_event():
    bundle = build_uom_agent(
        Settings(
            _env_file=None,
            ai_provider="adk",
            gemini_api_key="test-api-key",
            gemini_model="gemini-3.6-flash",
        )
    )
    provider = AdkInferenceProvider(bundle)
    sessions = FakeSessionService()
    provider.session_service = sessions
    provider.runner = FakeRunner()
    request = InferenceRequest(item_desc_eng="FLOUR 1 KG")

    first = await provider.infer(request)
    second = await provider.infer(request)

    assert first.result.measurement.uom == "KG"
    assert first.metadata.provider == "google-adk"
    assert first.metadata.prompt_sha256 == bundle.prompt_sha256
    assert first.metadata.session_id != second.metadata.session_id
    assert len(sessions.created) == len(sessions.deleted) == 2


@pytest.mark.asyncio
async def test_adk_provider_accepts_valid_pack_only_result():
    bundle = build_uom_agent(
        Settings(
            _env_file=None,
            ai_provider="adk",
            gemini_api_key="test-api-key",
            gemini_model="gemini-3.6-flash",
        )
    )
    provider = AdkInferenceProvider(bundle)
    provider.session_service = FakeSessionService()
    provider.runner = PackFinalRunner()
    request = InferenceRequest(
        task="PACK_ONLY",
        known_measurement={"value": 500, "uom": "ML"},
        item_desc_eng="JUICE ASSORTED 4'S 500ML",
    )

    response = await provider.infer(request)

    assert response.result.status == "PACK_PROPOSAL"
    assert response.result.pack_size == 4


def adk_provider(runner):
    bundle = build_uom_agent(Settings(
        _env_file=None, ai_provider="adk", gemini_api_key="test-api-key", gemini_model="gemini-3.6-flash",
    ))
    provider = AdkInferenceProvider(bundle)
    provider.session_service = FakeSessionService()
    provider.runner = runner
    return provider


class RateLimited(Exception):
    code = 429


class V3Event(FinalEvent):
    def __init__(self, payload, prompt_tokens, answer_tokens):
        super().__init__(payload)
        self.usage_metadata = types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt_tokens, candidates_token_count=answer_tokens,
        )


class FlakyV3Runner:
    """Rate-limited twice, then a contract-v3 answer with roles and no reason code."""

    def __init__(self):
        self.calls = 0

    async def run_async(self, **values):
        self.calls += 1
        if self.calls <= 2:
            raise RateLimited("429 RESOURCE_EXHAUSTED")
        yield V3Event({
            "status": "NOT_IN_DESCRIPTION",
            "other_measurements": [{
                "value": "1", "uom": "L", "field": "item_desc_eng", "fragment": "1L",
                "role": "CAPACITY_OR_RANGE",
            }],
            "rationale": "1L is the capacity of the box.",
        }, 1200, 45)


@pytest.mark.asyncio
async def test_rate_limits_are_retried_with_backoff_and_tokens_are_recorded(monkeypatch):
    from app.agents import adk_provider as module
    delays = []

    async def no_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    runner = FlakyV3Runner()
    provider = adk_provider(runner)

    response = await provider.infer(InferenceRequest(item_desc_eng="1L MICROWAVE BOX", category="Baking Aids"))

    assert runner.calls == 3 and len(delays) == 2
    assert 0.75 <= delays[0] <= 1.5 and 1.5 <= delays[1] <= 3.0  # jittered, growing
    assert response.result.status == "NOT_IN_DESCRIPTION"
    assert response.result.reason_code == "NOT_IN_DESCRIPTION"  # derived, not supplied
    assert response.result.other_measurements[0].role == "CAPACITY_OR_RANGE"
    assert (response.metadata.input_tokens, response.metadata.output_tokens) == (1200, 45)
    assert response.metadata.prompt_version == "uom-inference-v4.3"
    # Sessions are cleaned up even for the failed attempts.
    assert len(provider.session_service.created) == len(provider.session_service.deleted) == 3


class BrokenRunner:
    calls = 0

    async def run_async(self, **values):
        BrokenRunner.calls += 1
        raise ValueError("schema bug")
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_a_non_transient_failure_is_not_retried():
    provider = adk_provider(BrokenRunner())
    with pytest.raises(ValueError):
        await provider.infer(InferenceRequest(item_desc_eng="FLOUR 1 KG"))
    assert BrokenRunner.calls == 1


class RepairableRunner:
    def __init__(self):
        self.prompts = []

    async def run_async(self, **values):
        prompt = values["new_message"].parts[0].text
        self.prompts.append(prompt)
        fragment = "2 KG" if len(self.prompts) == 1 else "1 KG"
        yield FinalEvent({
            "status": "PROPOSAL",
            "measurement": {
                "value": "1", "uom": "KG", "field": "item_desc_eng",
                "fragment": fragment,
            },
        })


@pytest.mark.asyncio
async def test_invalid_structured_answer_gets_one_explicit_repair_retry():
    runner = RepairableRunner()
    provider = adk_provider(runner)

    response = await provider.infer(InferenceRequest(item_desc_eng="FLOUR 1 KG"))

    assert response.result.measurement.fragment == "1 KG"
    assert response.metadata.attempt_count == 2
    assert len(response.metadata.validation_errors) == 1
    assert "evidence fragment is absent" in response.metadata.validation_errors[0]
    first_request = json.loads(runner.prompts[0])
    repair_request = json.loads(runner.prompts[1])
    assert "repair_attempt" not in first_request
    assert repair_request["repair_attempt"] == 2
    assert "evidence fragment is absent" in repair_request["repair_validation_error"]


class AlwaysInvalidRunner:
    async def run_async(self, **values):
        yield FinalEvent({
            "status": "PROPOSAL",
            "measurement": {
                "value": "1", "uom": "KG", "field": "item_desc_eng",
                "fragment": "missing",
            },
        })


@pytest.mark.asyncio
async def test_two_invalid_answers_expose_both_validation_attempts():
    provider = adk_provider(AlwaysInvalidRunner())

    with pytest.raises(InvalidInferenceResponseError) as raised:
        await provider.infer(InferenceRequest(item_desc_eng="FLOUR 1 KG"))

    assert len(raised.value.validation_errors) == 2
    assert all("evidence fragment is absent" in error for error in raised.value.validation_errors)


def test_transient_errors_are_recognised_and_backoff_is_capped():
    from app.agents.adk_provider import (
        BACKOFF_CAP_SECONDS,
        backoff_seconds,
        is_output_schema_validation_error,
        is_transient,
    )
    from app.agents.provider import InferenceResult
    assert is_transient(RateLimited("x")) and is_transient(RuntimeError("503 UNAVAILABLE"))
    assert not is_transient(ValueError("bad schema"))
    assert all(backoff_seconds(retry) <= BACKOFF_CAP_SECONDS for retry in range(12))
    try:
        InferenceResult.model_validate({
            "status": "NOT_IN_DESCRIPTION",
            "reason_code": "EXPLICIT_PACK_COUNT",
        })
    except ValueError as exc:
        assert is_output_schema_validation_error(exc)


def test_evidence_may_never_be_cited_from_category_context():
    from app.agents.provider import InferenceResult, validate_evidence
    request = InferenceRequest(item_desc_eng="GREEN TEA", category="Tea 500G")
    with pytest.raises(ValueError):
        InferenceResult.model_validate({
            "status": "PROPOSAL",
            "measurement": {"value": "500", "uom": "G", "field": "category", "fragment": "500G"},
        })
    result = InferenceResult.model_validate({
        "status": "PROPOSAL",
        "measurement": {"value": "500", "uom": "G", "field": "item_desc_eng", "fragment": "500G"},
    })
    with pytest.raises(ValueError, match="absent"):
        validate_evidence(request, result)
