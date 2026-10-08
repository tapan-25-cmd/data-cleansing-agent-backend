"""The blind K/L/M checker: a second, stronger model works out the right unit size, unit and
pack size for one product from the raw data in the file, on its own. It is never shown what
the cleansing tool did for the row, nor the row's group, so its answer cannot lean on the
tool's. A deterministic comparison afterwards says whether the tool's values are right."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

from app.agents.judge import QUOTABLE_FIELDS, Values
from app.config import Settings

logger = logging.getLogger(__name__)

KLM_CHECKER_VERSION = "uom-klm-checker-v1"
PROMPT_PATH = Path(__file__).with_name("prompts") / "uom_klm_checker_v1.md"
APP_NAME = "uom_klm_checker"
USER_ID = "uom-klm-checker-worker"

Decision = Literal["AS_IS", "VALUES", "NEEDS_PERSON"]


class CheckRequest(BaseModel):
    """Everything the tool had, and nothing the tool produced."""
    task: Literal["WORK_OUT_KLM"] = "WORK_OUT_KLM"
    descriptions: dict[str, str | None]
    category: str | None = None
    subcategory: str | None = None
    category_kind: Literal["LIQUID", "MIXED", "UNKNOWN"] = "UNKNOWN"
    legacy: Values | None = None
    excel: Values
    repair_attempt: Literal[2] | None = None
    repair_validation_error: str | None = Field(default=None, max_length=1000)


class Evidence(BaseModel):
    field: str
    fragment: str = Field(min_length=1, max_length=120)


class CheckResult(BaseModel):
    decision: Decision
    size: str | None = None
    uom: Literal["GM", "ML", "EA"] | None = None
    pack_size: str | None = None
    evidence: list[Evidence] = Field(default_factory=list, max_length=4)
    reason: str = Field(max_length=400)
    confidence: Literal["HIGH", "MEDIUM", "LOW"] = "LOW"


def validate_check(request: CheckRequest, result: CheckResult) -> None:
    """Values go with the decision, and a quote must really be in the field it names."""
    if result.decision == "NEEDS_PERSON":
        if result.size or result.uom or result.pack_size:
            raise ValueError("NEEDS_PERSON gives no values")
    elif not (result.size and result.uom):
        raise ValueError(f"{result.decision} needs a size and a unit")
    for item in result.evidence:
        if item.field not in QUOTABLE_FIELDS:
            raise ValueError(f"evidence must come from an item or web description, not {item.field}")
        if item.fragment not in (request.descriptions.get(item.field) or ""):
            raise ValueError(f"fragment {item.fragment!r} is not in {item.field}")


class InvalidCheckResponse(RuntimeError):
    pass


@dataclass(frozen=True)
class CheckResponse:
    result: CheckResult
    model_id: str
    prompt_version: str
    prompt_sha256: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    attempts: int


def load_prompt() -> tuple[str, str]:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    return prompt, sha256(prompt.encode("utf-8")).hexdigest()


class MockKlmChecker:
    """Deterministic stand-in for tests: Excel is right as it stands, unless the description
    says CHANGE (then 999 GM × 1) or PERSON (then a person is needed)."""
    model_id = "mock-klm-checker"

    async def check(self, request: CheckRequest) -> CheckResponse:
        text = " ".join(v or "" for k, v in request.descriptions.items() if k in QUOTABLE_FIELDS)
        if "PERSON" in text or not (request.excel.size and request.excel.uom):
            result = CheckResult(decision="NEEDS_PERSON", reason="mock: a person is needed")
        elif "CHANGE" in text:
            result = CheckResult(decision="VALUES", size="999", uom="GM", pack_size="1", reason="mock: different values", confidence="HIGH")
        else:
            uom = request.excel.uom if request.excel.uom in ("GM", "ML", "EA") else "GM"
            result = CheckResult(decision="AS_IS", size=request.excel.size, uom=uom, pack_size=request.excel.pack_size or "1",
                                 reason="mock: right as it stands", confidence="HIGH")
        return CheckResponse(result, self.model_id, KLM_CHECKER_VERSION, "mock", 1, 0, 0, 1)


class AdkKlmChecker:
    """One isolated session per row, one bounded schema-repair retry, two retries on a
    timeout or provider error. Uses JUDGE_MODEL, a different and stronger model than the
    worker's, so the two do not share their mistakes."""

    def __init__(self, settings: Settings):
        from google import genai
        from google.adk.agents import LlmAgent
        from google.adk.apps import App
        from google.adk.models import Gemini
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types

        model_id = settings.judge_model or settings.gemini_model
        if not settings.gemini_api_key or not model_id:
            raise ValueError("GEMINI_API_KEY and JUDGE_MODEL (or GEMINI_MODEL) are required")
        prompt, self.prompt_sha256 = load_prompt()
        client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
        model = Gemini(model=model_id, client=client, retry_options=types.HttpRetryOptions(attempts=3))
        self.agent = LlmAgent(
            name="uom_klm_checker", description="Works out the right unit size, unit and pack size from the raw item data.",
            model=model, instruction=prompt, input_schema=CheckRequest, output_schema=CheckResult, output_key="klm_check",
            generate_content_config=types.GenerateContentConfig(
                temperature=0, max_output_tokens=8192,
                thinking_config=types.ThinkingConfig(thinking_level="HIGH"),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)),
            tools=[], mode="chat", include_contents="none",
        )
        self.model_id = model_id
        self.timeout_seconds = settings.judge_timeout_seconds
        self.session_service = InMemorySessionService()
        self.runner = Runner(app=App(name=APP_NAME, root_agent=self.agent), session_service=self.session_service)
        self._types = types

    async def check(self, request: CheckRequest) -> CheckResponse:
        errors: list[str] = []
        attempt = transient = 0
        while True:
            attempt += 1
            try:
                return await self._once(request, min(attempt, 2), errors)
            except InvalidCheckResponse as exc:
                errors.append(str(exc))
                if len(errors) >= 2:
                    raise
            except Exception:  # noqa: BLE001 - timeouts and provider errors
                transient += 1
                if transient > 2:
                    raise
                await asyncio.sleep(5 * transient)

    async def _once(self, request: CheckRequest, attempt: int, errors: list[str]) -> CheckResponse:
        session_id = str(uuid4())
        started = perf_counter()
        await self.session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
        try:
            sent = request if not errors else request.model_copy(update={"repair_attempt": 2, "repair_validation_error": errors[-1][:1000]})
            content = self._types.Content(role="user", parts=[self._types.Part(text=sent.model_dump_json(exclude_none=True))])
            final_text: str | None = None
            input_tokens = output_tokens = 0
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    async for event in self.runner.run_async(user_id=USER_ID, session_id=session_id, new_message=content):
                        usage = getattr(event, "usage_metadata", None)
                        if usage is not None:
                            input_tokens += getattr(usage, "prompt_token_count", 0) or 0
                            output_tokens += (getattr(usage, "candidates_token_count", 0) or 0) + (getattr(usage, "thoughts_token_count", 0) or 0)
                        if event.is_final_response() and event.content and event.content.parts:
                            final_text = "".join(part.text or "" for part in event.content.parts)
            except ValidationError as exc:
                raise InvalidCheckResponse(f"output failed validation: {exc}") from exc
            if not final_text:
                raise RuntimeError("ADK run completed without a final structured response")
            try:
                result = CheckResult.model_validate_json(final_text)
                validate_check(request, result)
            except ValueError as exc:
                raise InvalidCheckResponse(f"invalid answer: {exc}") from exc
            return CheckResponse(result, self.model_id, KLM_CHECKER_VERSION, self.prompt_sha256,
                                 round((perf_counter() - started) * 1000), input_tokens, output_tokens, attempt)
        finally:
            try:
                await self.session_service.delete_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
            except Exception:  # noqa: BLE001
                logger.debug("klm checker session cleanup failed", exc_info=True)


def create_klm_checker(settings: Settings) -> Any:
    if settings.ai_provider.strip().lower() == "mock":
        return MockKlmChecker()
    return AdkKlmChecker(settings)
