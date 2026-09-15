from app.agents.mock_provider import MockInferenceProvider
from app.agents.provider import InferenceProvider
from app.config import Settings


class AgentConfigurationError(RuntimeError):
    pass


def create_inference_provider(settings: Settings) -> InferenceProvider:
    provider = settings.ai_provider.strip().lower()
    if provider == "mock":
        return MockInferenceProvider()
    if provider != "adk":
        raise AgentConfigurationError(f"Unsupported AI_PROVIDER: {settings.ai_provider}")

    missing = [
        name
        for name, value in (
            ("GEMINI_API_KEY", settings.gemini_api_key),
            ("GEMINI_MODEL", settings.gemini_model),
        )
        if not value
    ]
    if missing:
        raise AgentConfigurationError(
            "ADK Gemini API configuration is incomplete: " + ", ".join(missing)
        )

    try:
        from app.agents.adk_provider import AdkInferenceProvider
        from app.agents.uom_inference_agent import build_uom_agent
    except ImportError as exc:
        raise AgentConfigurationError(
            "Google ADK is not installed; install the backend with the 'ai' extra"
        ) from exc
    try:
        return AdkInferenceProvider(
            build_uom_agent(settings),
            timeout_seconds=settings.ai_timeout_seconds,
        )
    except Exception as exc:
        raise AgentConfigurationError(f"Unable to initialize Google ADK: {exc}") from exc
