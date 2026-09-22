import asyncio
import json
import logging
import random
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
# Rate limits and brief outages are retried with exponential backoff and jitter, so a
# burst of concurrent products does not retry in lockstep. Anything else fails fast.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
MAX_TRANSIENT_RETRIES = 4
BACKOFF_BASE_SECONDS = 1.5
BACKOFF_CAP_SECONDS = 30.0


def is_transient(error: BaseException) -> bool:
    code = getattr(error, "code", None) or getattr(error, "status_code", None)
    if isinstance(code, int) and code in RETRYABLE_STATUS:
        return True
    text = f"{type(error).__name__} {error}".upper()
    return any(marker in text for marker in ("RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE_EXCEEDED", " 429"))


def backoff_seconds(retry: int) -> float:
    ceiling = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** retry))
    return random.uniform(ceiling / 2, ceiling)
logger = logging.getLogger(__name__)


def _log_event(event: str, **fields: object) -> None:
    logger.info(json.dumps({"event": event, **fields}, default=str, separators=(",", ":")))


class AdkInferenceProvider:
    def __init__(self, bundle: UomAgentBundle, timeout_seconds: float = 60):
        self.bundle = bundle
        self.timeout_seconds = timeout_seconds
        self.session_service = InMemorySessionService()
        self.runner = Runner(app=bundle.app, session_service=self.session_service)

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        last_invalid: InvalidInferenceResponseError | None = None
        for attempt in range(1, 3):
            try:
                return await self._infer_with_backoff(request, attempt)
            except InvalidInferenceResponseError as exc:
                last_invalid = exc
                _log_event(
                    "ai.invalid_response",
                    attempt=attempt,
                    model_id=self.bundle.model_id,
                    error=str(exc),
                )
                if attempt == 2:
                    raise
        raise last_invalid or InferenceProviderError("ADK inference failed")

    async def _infer_with_backoff(self, request: InferenceRequest, attempt: int) -> InferenceResponse:
        for retry in range(MAX_TRANSIENT_RETRIES + 1):
            try:
                return await self._infer_once(request, attempt + retry)
            except InvalidInferenceResponseError:
                raise
            except Exception as exc:
                if retry == MAX_TRANSIENT_RETRIES or not is_transient(exc):
                    raise
                delay = backoff_seconds(retry)
                _log_event(
                    "ai.transient_error_retry", retry=retry + 1, delay_seconds=round(delay, 1),
                    error_type=type(exc).__name__, model_id=self.bundle.model_id,
                )
                await asyncio.sleep(delay)
        raise InferenceProviderError("ADK inference failed")  # pragma: no cover

    async def _infer_once(self, request: InferenceRequest, attempt: int) -> InferenceResponse:
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
            input_tokens = output_tokens = 0
            async with asyncio.timeout(self.timeout_seconds):
                async for event in self.runner.run_async(
                    user_id=WORKER_USER_ID,
                    session_id=session_id,
                    new_message=content,
                ):
                    usage = getattr(event, "usage_metadata", None)
                    if usage is not None:
                        input_tokens += getattr(usage, "prompt_token_count", 0) or 0
                        output_tokens += (getattr(usage, "candidates_token_count", 0) or 0) + (
                            getattr(usage, "thoughts_token_count", 0) or 0
                        )
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
            latency_ms = round((perf_counter() - started) * 1000)
            _log_event(
                "ai.inference_completed",
                attempt=attempt,
                model_id=self.bundle.model_id,
                session_id=session_id,
                latency_ms=latency_ms,
                status=result.status,
                reason_code=result.reason_code,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
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
                    attempt_count=attempt,
                    latency_ms=latency_ms,
                    session_id=session_id,
                    input_tokens=input_tokens or None,
                    output_tokens=output_tokens or None,
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
