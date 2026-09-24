"""Lane A reasoning trial: a second, separate AI task that sees every source for a row and
reconciles them the way a reviewer would. Results are stored for scoring; nothing here writes
to the workbook, the suggestions or the labels.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError, model_validator

from app.config import Settings

logger = logging.getLogger(__name__)

RECONCILE_PROMPT_VERSION = "uom-reconcile-v2"
RECONCILE_PROMPT_PATH = Path(__file__).with_name("prompts") / "uom_reconcile_v2.md"
RECONCILE_APP_NAME = "uom_reconcile_trial"
RECONCILE_AGENT_NAME = "uom_reconcile_agent"
WORKER_USER_ID = "uom-reconcile-worker"

DESCRIPTION_FIELDS = (
    "item_brand_eng", "item_brand_local_lang", "item_desc_eng", "item_desc_local_lang",
    "web_description_eng", "web_description_chi",
)


class SourceValue(BaseModel):
    size: str | None = None
    uom: str | None = None
    pack_size: str | None = None
    total: str | None = None


class RuleOutcome(BaseModel):
    label: str
    comment: str = Field(max_length=1200)
    suggestion: SourceValue | None = None


class ReconcileRequest(BaseModel):
    task: Literal["RECONCILE"] = "RECONCILE"
    descriptions: dict[str, str | None]
    category: str | None = None
    subcategory: str | None = None
    category_kind: Literal["LIQUID", "MIXED", "UNKNOWN"] = "UNKNOWN"
    legacy: SourceValue | None = None
    excel: SourceValue
    rules: RuleOutcome
    repair_attempt: Literal[2] | None = None
    repair_validation_error: str | None = Field(default=None, max_length=1000)


class SourceMeaning(BaseModel):
    source: Literal["LEGACY", "EXCEL", "ITEM_DESCRIPTION", "WEB_DESCRIPTION", "BRAND"]
    meaning: Literal["PER_PIECE", "WHOLE_PACK", "PACKAGE_COUNT", "PIECE_COUNT", "NOT_A_SIZE", "SILENT", "UNCLEAR"]
    note: str = Field(default="", max_length=240)


class Evidence(BaseModel):
    field: str
    fragment: str = Field(min_length=1, max_length=120)


class Proposed(BaseModel):
    standard_size: Decimal | None = None
    standard_uom: str | None = None
    standard_pack_size: Decimal | None = None

    @model_validator(mode="after")
    def positive(self) -> "Proposed":
        for value in (self.standard_size, self.standard_pack_size):
            if value is not None and (not value.is_finite() or value <= 0):
                raise ValueError("proposed quantities must be positive")
        if self.standard_uom:
            self.standard_uom = self.standard_uom.strip().upper()
        return self


class ReconcileResult(BaseModel):
    product_unit: str = Field(min_length=1, max_length=200)
    sources: list[SourceMeaning] = Field(default_factory=list)
    verdict: Literal["EXCEL_RIGHT", "LEGACY_RIGHT", "DESCRIPTION_RIGHT", "COMBINED", "CANNOT_TELL"]
    proposed: Proposed | None = None
    explanation: str = Field(min_length=1, max_length=600)
    evidence: list[Evidence] = Field(default_factory=list)
    needs_business_rule: bool = False
    business_rule_note: str | None = Field(default=None, max_length=300)
    used_product_knowledge: str | None = Field(default=None, max_length=200)
    confidence: Literal["HIGH", "MEDIUM", "LOW"] = "LOW"

    @model_validator(mode="after")
    def shape(self) -> "ReconcileResult":
        if self.verdict == "CANNOT_TELL":
            self.proposed = None
        elif self.proposed is None or self.proposed.standard_size is None or not self.proposed.standard_uom:
            raise ValueError("a verdict other than CANNOT_TELL needs a complete proposed size and unit")
        return self


def validate_evidence(request: ReconcileRequest, result: ReconcileResult) -> None:
    for item in result.evidence:
        if item.field not in DESCRIPTION_FIELDS:
            raise ValueError(f"evidence must come from a description field, not {item.field}")
        text = request.descriptions.get(item.field) or ""
        if item.fragment not in text:
            raise ValueError(f"fragment {item.fragment!r} is not in {item.field}")


class InvalidReconcileResponse(RuntimeError):
    pass


@dataclass(frozen=True)
class ReconcileResponse:
    result: ReconcileResult
    model_id: str
    prompt_version: str
    prompt_sha256: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    attempts: int


def load_reconcile_prompt() -> tuple[str, str]:
    prompt = RECONCILE_PROMPT_PATH.read_text(encoding="utf-8")
    return prompt, sha256(prompt.encode("utf-8")).hexdigest()


class AdkReconcileProvider:
    """One isolated session per row, one bounded schema-repair retry."""

    def __init__(self, settings: Settings):
        from google import genai
        from google.adk.agents import LlmAgent
        from google.adk.apps import App
        from google.adk.models import Gemini
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types

        if not settings.gemini_api_key or not settings.gemini_model:
            raise ValueError("GEMINI_API_KEY and GEMINI_MODEL are required")
        prompt, self.prompt_sha256 = load_reconcile_prompt()
        client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
        model = Gemini(model=settings.gemini_model, client=client, retry_options=types.HttpRetryOptions(attempts=3))
        self.agent = LlmAgent(
            name=RECONCILE_AGENT_NAME,
            description="Reconciles size, unit and pack size across sources the way a product-data reviewer would.",
            model=model, instruction=prompt,
            input_schema=ReconcileRequest, output_schema=ReconcileResult, output_key="reconcile_result",
            generate_content_config=types.GenerateContentConfig(
                temperature=0, max_output_tokens=4096,
                thinking_config=types.ThinkingConfig(thinking_level="LOW"),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
            tools=[], mode="chat", include_contents="none",
        )
        self.model_id = settings.gemini_model
        self.timeout_seconds = settings.ai_timeout_seconds
        self.session_service = InMemorySessionService()
        self.runner = Runner(app=App(name=RECONCILE_APP_NAME, root_agent=self.agent), session_service=self.session_service)
        self._types = types

    async def reconcile(self, request: ReconcileRequest) -> ReconcileResponse:
        errors: list[str] = []
        for attempt in (1, 2):
            try:
                return await self._once(request, attempt, errors)
            except InvalidReconcileResponse as exc:
                errors.append(str(exc))
                if attempt == 2:
                    raise
        raise InvalidReconcileResponse("unreachable")  # pragma: no cover

    async def _once(self, request: ReconcileRequest, attempt: int, errors: list[str]) -> ReconcileResponse:
        session_id = str(uuid4())
        started = perf_counter()
        await self.session_service.create_session(app_name=RECONCILE_APP_NAME, user_id=WORKER_USER_ID, session_id=session_id)
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
                raise InvalidReconcileResponse(f"output failed validation: {exc}") from exc
            if not final_text:
                raise RuntimeError("ADK run completed without a final structured response")
            try:
                result = ReconcileResult.model_validate_json(final_text)
                validate_evidence(request, result)
            except ValueError as exc:
                raise InvalidReconcileResponse(f"invalid reconcile result: {exc}") from exc
            return ReconcileResponse(
                result=result, model_id=self.model_id, prompt_version=RECONCILE_PROMPT_VERSION,
                prompt_sha256=self.prompt_sha256, latency_ms=round((perf_counter() - started) * 1000),
                input_tokens=input_tokens, output_tokens=output_tokens, attempts=attempt,
            )
        finally:
            try:
                await self.session_service.delete_session(app_name=RECONCILE_APP_NAME, user_id=WORKER_USER_ID, session_id=session_id)
            except Exception:  # noqa: BLE001
                logger.debug("reconcile session cleanup failed", exc_info=True)
