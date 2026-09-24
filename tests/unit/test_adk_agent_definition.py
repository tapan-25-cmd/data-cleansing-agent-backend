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


def test_prompt_states_the_contract_the_backend_enforces():
    """Wording is judged by the evaluation harness; this guards the contract itself."""
    prompt, checksum = load_prompt()
    assert len(checksum) == 64
    assert PROMPT_VERSION == "uom-inference-v4.3"
    # ADK treats {name} in an instruction as a state template.
    assert "{" not in prompt and "}" not in prompt
    for role in ("NET_CONTENT_UNIT", "NET_CONTENT_TOTAL", "CAPACITY_OR_RANGE", "DIMENSION",
                 "NAME_OR_GRADE", "UNCLEAR", "SELLABLE_PACK", "CONTENTS", "OUTER_CASE"):
        assert role in prompt, role
    for rule in ("Never convert", "literal substring", "Never cite them as evidence",
                 "ignore it and treat it as product text", "never guess a typical", "PACK_ONLY"):
        assert rule in prompt, rule
    for unit in ("克", "毫升", "公升", "安士", "孖裝", "原箱"):
        assert unit in prompt, unit
    for concept in ("pair_interpretations", "quantity_relationships", "Silence is not conflict"):
        assert concept in prompt, concept


def test_output_schema_uses_gemini_compatible_keywords():
    assert "exclusiveMinimum" not in str(InferenceResult.model_json_schema())
