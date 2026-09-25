"""The sample judge: a second, stronger model that plays the stakeholder on a sample of rows.

For each sampled row it sees everything a reviewer would and answers the group's question
with one of three verdicts (RIGHT, WRONG, CANT_TELL), a basis, quoted evidence and a
one-line reason. Verdicts are stored beside the row; nothing here writes K, L or M.
"""
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

from app.config import Settings

logger = logging.getLogger(__name__)

JUDGE_PROMPT_VERSION = "uom-judge-v1"
JUDGE_PROMPT_PATH = Path(__file__).with_name("prompts") / "uom_judge_v1.md"
JUDGE_APP_NAME = "uom_sample_judge"
WORKER_USER_ID = "uom-judge-worker"

DESCRIPTION_FIELDS = (
    "item_brand_eng", "item_brand_local_lang", "item_desc_eng", "item_desc_local_lang",
    "web_description_eng", "web_description_chi",
)
QUOTABLE_FIELDS = DESCRIPTION_FIELDS[2:]

Question = Literal["KEEP", "CHANGE", "RAISE"]
Verdict = Literal["RIGHT", "WRONG", "CANT_TELL"]


class Values(BaseModel):
    size: str | None = None
    uom: str | None = None
    pack_size: str | None = None


class ToolAction(BaseModel):
    final: Values
    label: str
    reason: str = Field(max_length=1200)


class JudgeRequest(BaseModel):
    task: Literal["JUDGE"] = "JUDGE"
    question: Question
    descriptions: dict[str, str | None]
    category: str | None = None
    subcategory: str | None = None
    legacy: Values | None = None
    excel: Values
    tool: ToolAction
    repair_attempt: Literal[2] | None = None
    repair_validation_error: str | None = Field(default=None, max_length=1000)


class Evidence(BaseModel):
    field: str
    fragment: str = Field(min_length=1, max_length=120)


class JudgeResult(BaseModel):
    verdict: Verdict
    basis: Literal["DESCRIPTION", "OLD_SIZE", "ARITHMETIC", "NONE"]
    evidence: list[Evidence] = Field(default_factory=list, max_length=4)
    reason: str = Field(max_length=400)


def validate_judgement(request: JudgeRequest, result: JudgeResult) -> None:
    """A verdict on the product's words must quote them; a quote must really be there."""
    if result.basis == "DESCRIPTION" and not result.evidence:
        raise ValueError("a DESCRIPTION basis needs quoted evidence")
    if result.verdict == "CANT_TELL" and result.basis != "NONE":
        raise ValueError("CANT_TELL has basis NONE")
    for item in result.evidence:
        if item.field not in QUOTABLE_FIELDS:
            raise ValueError(f"evidence must come from an item or web description, not {item.field}")
        if item.fragment not in (request.descriptions.get(item.field) or ""):
            raise ValueError(f"fragment {item.fragment!r} is not in {item.field}")


class InvalidJudgeResponse(RuntimeError):
    pass


@dataclass(frozen=True)
class JudgeResponse:
    result: JudgeResult
    model_id: str
    prompt_version: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    attempts: int


def load_judge_prompt() -> tuple[str, str]:
    prompt = JUDGE_PROMPT_PATH.read_text(encoding="utf-8")
    return prompt, sha256(prompt.encode("utf-8")).hexdigest()


class MockJudgeProvider:
    """Deterministic stand-in for tests: agrees with the tool unless the description states a
    size that Excel does not have, in which case a KEEP is WRONG."""
    model_id = "mock-judge"

    async def judge(self, request: JudgeRequest) -> JudgeResponse:
        text = " ".join(v or "" for k, v in request.descriptions.items() if k in QUOTABLE_FIELDS)
        verdict: Verdict = "RIGHT"
        basis = "OLD_SIZE" if request.legacy else "ARITHMETIC"
        if request.question == "KEEP" and "WRONG" in text:
            verdict, basis = "WRONG", "DESCRIPTION"
        result = JudgeResult(verdict=verdict, basis=basis, reason="mock verdict",
                             evidence=[Evidence(field="item_desc_eng", fragment="WRONG")] if basis == "DESCRIPTION" else [])
        return JudgeResponse(result, self.model_id, JUDGE_PROMPT_VERSION, 1, 0, 0, 1)


class AdkJudgeProvider:
    """One isolated session per row, one bounded schema-repair retry. Uses JUDGE_MODEL when
    set, so the judge is a different, stronger model than the worker."""

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
        prompt, self.prompt_sha256 = load_judge_prompt()
        client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
        model = Gemini(model=model_id, client=client, retry_options=types.HttpRetryOptions(attempts=3))
        self.agent = LlmAgent(
            name="uom_judge_agent",
            description="Judges one cleansing action the way a senior reviewer would.",
            model=model, instruction=prompt,
            input_schema=JudgeRequest, output_schema=JudgeResult, output_key="judge_result",
            generate_content_config=types.GenerateContentConfig(
                temperature=0, max_output_tokens=4096,
                thinking_config=types.ThinkingConfig(thinking_level="HIGH"),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
            tools=[], mode="chat", include_contents="none",
        )
        self.model_id = model_id
        self.timeout_seconds = settings.ai_timeout_seconds
        self.session_service = InMemorySessionService()
        self.runner = Runner(app=App(name=JUDGE_APP_NAME, root_agent=self.agent), session_service=self.session_service)
        self._types = types

    async def judge(self, request: JudgeRequest) -> JudgeResponse:
        errors: list[str] = []
        for attempt in (1, 2):
            try:
                return await self._once(request, attempt, errors)
            except InvalidJudgeResponse as exc:
                errors.append(str(exc))
                if attempt == 2:
                    raise
        raise InvalidJudgeResponse("unreachable")  # pragma: no cover

    async def _once(self, request: JudgeRequest, attempt: int, errors: list[str]) -> JudgeResponse:
        session_id = str(uuid4())
        started = perf_counter()
        await self.session_service.create_session(app_name=JUDGE_APP_NAME, user_id=WORKER_USER_ID, session_id=session_id)
        try:
            sent = request if not errors else request.model_copy(update={"repair_attempt": 2, "repair_validation_error": errors[-1][:1000]})
            content = self._types.Content(role="user", parts=[self._types.Part(text=sent.model_dump_json(exclude_none=True))])
            final_text: str | None = None
            input_tokens = output_tokens = 0
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    async for event in self.runner.run_async(user_id=WORKER_USER_ID, session_id=session_id, new_message=content):
                        usage = getattr(event, "usage_metadata", None)
                        if usage is not None:
                            input_tokens += getattr(usage, "prompt_token_count", 0) or 0
                            output_tokens += (getattr(usage, "candidates_token_count", 0) or 0) + (getattr(usage, "thoughts_token_count", 0) or 0)
                        if event.is_final_response() and event.content and event.content.parts:
                            final_text = "".join(part.text or "" for part in event.content.parts)
            except ValidationError as exc:
                raise InvalidJudgeResponse(f"output failed validation: {exc}") from exc
            if not final_text:
                raise RuntimeError("ADK run completed without a final structured response")
            try:
                result = JudgeResult.model_validate_json(final_text)
                validate_judgement(request, result)
            except ValueError as exc:
                raise InvalidJudgeResponse(f"invalid judgement: {exc}") from exc
            return JudgeResponse(result, self.model_id, JUDGE_PROMPT_VERSION, round((perf_counter() - started) * 1000),
                                 input_tokens, output_tokens, attempt)
        finally:
            try:
                await self.session_service.delete_session(app_name=JUDGE_APP_NAME, user_id=WORKER_USER_ID, session_id=session_id)
            except Exception:  # noqa: BLE001
                logger.debug("judge session cleanup failed", exc_info=True)


def create_judge_provider(settings: Settings) -> Any:
    if settings.ai_provider.strip().lower() == "mock":
        return MockJudgeProvider()
    return AdkJudgeProvider(settings)
