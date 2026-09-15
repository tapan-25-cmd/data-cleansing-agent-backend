import pytest

from app.agents.factory import AgentConfigurationError, create_inference_provider
from app.agents.mock_provider import MockInferenceProvider
from app.config import Settings


def settings(**values) -> Settings:
    return Settings(_env_file=None, **values)


def test_factory_selects_mock_without_importing_cloud_runtime():
    assert isinstance(create_inference_provider(settings(ai_provider="mock")), MockInferenceProvider)


def test_factory_rejects_unknown_provider():
    with pytest.raises(AgentConfigurationError, match="Unsupported"):
        create_inference_provider(settings(ai_provider="magic"))


def test_adk_configuration_fails_fast_when_required_values_are_missing():
    with pytest.raises(AgentConfigurationError, match="GEMINI_API_KEY"):
        create_inference_provider(
            settings(
                ai_provider="adk",
                gemini_api_key=None,
                gemini_model=None,
            )
        )


def test_factory_builds_real_adk_provider_with_api_key():
    provider = create_inference_provider(
        settings(
            ai_provider="adk",
            gemini_api_key="test-api-key",
            gemini_model="gemini-2.5-flash",
        )
    )

    assert type(provider).__name__ == "AdkInferenceProvider"
