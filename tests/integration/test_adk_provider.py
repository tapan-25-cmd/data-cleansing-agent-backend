import json

import pytest
from google.genai import types

from app.agents.adk_provider import AdkInferenceProvider
from app.agents.provider import InferenceRequest
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
