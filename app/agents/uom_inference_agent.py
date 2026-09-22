from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from google import genai
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

from app.agents.provider import InferenceRequest, InferenceResult
from app.agents.versions import AGENT_NAME, AGENT_VERSION, PROMPT_VERSION  # noqa: F401
from app.config import Settings

APP_NAME = "uom_cleansing"
PROMPT_PATH = Path(__file__).with_name("prompts") / "uom_inference_v3.md"


@dataclass(frozen=True)
class UomAgentBundle:
    agent: LlmAgent
    app: App
    model_id: str
    prompt_version: str
    prompt_sha256: str


def load_prompt() -> tuple[str, str]:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    return prompt, sha256(prompt.encode("utf-8")).hexdigest()


def build_uom_agent(settings: Settings) -> UomAgentBundle:
    if not settings.gemini_api_key:
        raise ValueError("GEMINI_API_KEY is required for the ADK provider")
    if not settings.gemini_model:
        raise ValueError("GEMINI_MODEL is required for the ADK provider")
    prompt, prompt_checksum = load_prompt()
    client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
    model = Gemini(
        model=settings.gemini_model,
        client=client,
        retry_options=types.HttpRetryOptions(attempts=3),
    )
    agent = LlmAgent(
        name=AGENT_NAME,
        description="Extracts explicit unit-size and pack evidence from permitted product text.",
        model=model,
        instruction=prompt,
        input_schema=InferenceRequest,
        output_schema=InferenceResult,
        output_key="uom_inference_result",
        generate_content_config=types.GenerateContentConfig(
            temperature=0,
            max_output_tokens=4096,
            thinking_config=types.ThinkingConfig(thinking_level="LOW"),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
        tools=[],
        # ADK 2.x root agents must use chat or task mode. Every product still gets
        # an isolated one-use session in AdkInferenceProvider.
        mode="chat",
        include_contents="none",
    )
    return UomAgentBundle(
        agent=agent,
        app=App(name=APP_NAME, root_agent=agent),
        model_id=settings.gemini_model,
        prompt_version=PROMPT_VERSION,
        prompt_sha256=prompt_checksum,
    )
