import asyncio
import logging
from importlib.metadata import version
from time import perf_counter
from uuid import uuid4

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agents.provider import (
    InferenceRequest,
    InferenceProviderError,
    InferenceResponse,
    InferenceResult,
    InvalidInferenceResponseError,
    ProviderMetadata,
    validate_evidence,
)
from app.agents.uom_inference_agent import AGENT_VERSION, APP_NAME, UomAgentBundle

WORKER_USER_ID = "uom-cleansing-worker"
logger = logging.getLogger(__name__)


class AdkInferenceProvider:
    def __init__(self, bundle: UomAgentBundle, timeout_seconds: float = 60):
        self.bundle = bundle
        self.timeout_seconds = timeout_seconds
        self.session_service = InMemorySessionService()
        self.runner = Runner(app=bundle.app, session_service=self.session_service)

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        session_id = str(uuid4())
        started = perf_counter()
        await self.session_service.create_session(
            app_name=APP_NAME,
            user_id=WORKER_USER_ID,
            session_id=session_id,
        )
        try:
            content = types.Content(
                role="user",
                parts=[types.Part(text=request.model_dump_json(exclude_none=True))],
            )
            final_text: str | None = None
            async with asyncio.timeout(self.timeout_seconds):
                async for event in self.runner.run_async(
                    user_id=WORKER_USER_ID,
                    session_id=session_id,
                    new_message=content,
                ):
                    if event.is_final_response() and event.content and event.content.parts:
                        final_text = "".join(part.text or "" for part in event.content.parts)
            if not final_text:
                raise InferenceProviderError("ADK run completed without a final structured response")
            try:
                result = InferenceResult.model_validate_json(final_text)
                validate_evidence(request, result)
            except ValueError as exc:
                raise InvalidInferenceResponseError(
                    f"ADK returned an invalid inference result: {exc}"
                ) from exc
            return InferenceResponse(
                result=result,
                metadata=ProviderMetadata(
                    provider="google-adk",
                    agent_name=self.bundle.agent.name,
                    agent_version=AGENT_VERSION,
                    prompt_version=self.bundle.prompt_version,
                    prompt_sha256=self.bundle.prompt_sha256,
                    model_id=self.bundle.model_id,
                    adk_version=version("google-adk"),
                    latency_ms=round((perf_counter() - started) * 1000),
                    session_id=session_id,
                ),
            )
        except TimeoutError as exc:
            raise InferenceProviderError(
                f"ADK inference exceeded {self.timeout_seconds:g} seconds"
            ) from exc
        finally:
            try:
                await self.session_service.delete_session(
                    app_name=APP_NAME,
                    user_id=WORKER_USER_ID,
                    session_id=session_id,
                )
            except Exception:
                logger.warning("Unable to delete ADK session %s", session_id, exc_info=True)
