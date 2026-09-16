from app.agents.provider import InferenceRequest, InferenceResult
from app.agents.uom_inference_agent import AGENT_NAME, PROMPT_VERSION, build_uom_agent, load_prompt
from app.config import Settings


def test_real_agent_is_isolated_structured_and_tool_free():
    bundle = build_uom_agent(
        Settings(
            _env_file=None,
            ai_provider="adk",
            gemini_api_key="test-api-key",
            gemini_model="gemini-3.6-flash",
        )
    )
    assert bundle.agent.name == AGENT_NAME
    assert bundle.agent.input_schema is InferenceRequest
    assert bundle.agent.output_schema is InferenceResult
    assert bundle.agent.tools == []
    assert bundle.agent.mode == "chat"
    assert bundle.agent.include_contents == "none"
    assert bundle.agent.generate_content_config.automatic_function_calling.disable is True
    assert bundle.prompt_version == PROMPT_VERSION
    assert len(bundle.prompt_sha256) == 64


def test_prompt_is_versioned_and_forbids_conversion():
    prompt, checksum = load_prompt()
    assert "Do not convert units" in prompt
    assert "Do not browse" in prompt
    assert "PACK_ONLY" in prompt
    assert "loose piece/content count" in prompt
    assert len(checksum) == 64


def test_output_schema_uses_gemini_compatible_keywords():
    assert "exclusiveMinimum" not in str(InferenceResult.model_json_schema())
