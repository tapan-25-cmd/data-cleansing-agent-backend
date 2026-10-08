"""The pack reader: a model looks at a product photo and reports the net quantity printed on
the pack, quoted as printed. It is shown the photo only, never our values or the shop's
text, so it cannot be led. Its reading is evidence: nothing here writes K, L or M."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from app.config import Settings

logger = logging.getLogger(__name__)

PACK_READER_VERSION = "pack-reader-v4"
APP_NAME = "uom_pack_reader"
USER_ID = "uom-pack-reader-worker"

INSTRUCTION = """You read the printed net quantity on a product pack from its photos.

You are given every photo in one product's gallery, each preceded by its label ("Photo 1",
"Photo 2", ...), and the product's brand and name with all numbers removed.

1. Check each photo shows that product. List in `other_product_photos` the numbers of any
   photo that clearly shows a different product. Serving suggestions, lifestyle shots and
   banners are not a different product; just ignore them.
2. From the photos that do show the product, report the net quantity printed on the pack:
   the net weight, net volume or piece count, and every packaging level (for example
   "4 x 200g", "16 bags, each containing 4 x 200g", "10 pieces", "500ml"). The quantity
   is often on the back or side.
   Quote the text exactly as printed in `printed`, and give in `photo` the number of the
   photo you read it from. Give `size` and `uom` for one smallest consumption unit.
   If there is one multiplier, put it in `inner_count`. If an outer case/bundle multiplier
   is also visible, put that in `outer_count`. Set `total_count` to inner_count multiplied
   by outer_count. Set legacy `count` equal to total_count. A missing multiplier is 1; do
   not invent one. If the case artwork directly states the total number of consumption
   units (for example "x24 cans"), that explicit figure is `total_count` even when it does
   not print the number of sleeves. When an inner pack is also explicit (for example
   "8x330 mL") and total_count divides exactly by inner_count, report the quotient as
   `outer_count` and explain that arithmetic in `note`. Example: "8x330 mL" plus "x24 cans"
   means size=330, inner_count=8, outer_count=3 and total_count=24. A bag marked
   "200g x 4" in a case marked "x16 bags" means
   size=200, uom=GM, inner_count=4, outer_count=16, total_count=64, count=64.
   Use uom GM for grams, KG, ML, L, EA for pieces, OZ, LB.
   When the pack shows both a net weight or volume and a piece count, report the weight
   or volume (look across all the photos for it) and mention the piece count in `note`;
   report a piece count alone only when no net weight or volume is printed anywhere.
3. Nutrient figures (protein 3.3g), serving sizes, prices, barcodes and dates are not the
   net quantity. If no photo shows a readable net quantity, set legible=false, leave the
   values empty and say why in `note`. Never guess.

Answer in JSON matching the schema."""


class PackReading(BaseModel):
    legible: bool
    printed: str | None = Field(default=None, max_length=120)
    size: str | None = None
    uom: Literal["GM", "KG", "ML", "L", "EA", "OZ", "LB"] | None = None
    count: int | None = None
    inner_count: int | None = None
    outer_count: int | None = None
    total_count: int | None = None
    confidence: Literal["HIGH", "MEDIUM", "LOW"] = "LOW"
    photo: int | None = None  # which photo (1-based) carried the quantity
    other_product_photos: list[int] = Field(default_factory=list)
    note: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def reconcile_pack_counts(self) -> "PackReading":
        """Keep the compatibility count deterministic after structured extraction."""
        inner = self.inner_count or 1
        outer = self.outer_count or 1
        calculated = inner * outer
        if self.total_count is None and (self.inner_count is not None or self.outer_count is not None):
            self.total_count = calculated
        if self.total_count is not None:
            self.count = self.total_count
        elif self.count is None and self.legible:
            self.count = 1
        return self


@dataclass(frozen=True)
class PackReadResponse:
    result: PackReading
    model_id: str
    version: str
    latency_ms: int
    input_tokens: int
    output_tokens: int


def image_type(data: bytes, declared: str | None) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return declared if declared and declared.startswith("image/") else "image/jpeg"


class MockPackReader:
    model_id = "mock-pack-reader"

    async def read(self, images: list[tuple[bytes, str]], product: str = "") -> PackReadResponse:
        return PackReadResponse(PackReading(legible=False, note="mock"), self.model_id, PACK_READER_VERSION, 1, 0, 0)


class AdkPackReader:
    """One isolated session per product. Uses the judge's model: it reads photos well and the
    volume is small."""

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
        client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
        model = Gemini(model=model_id, client=client, retry_options=types.HttpRetryOptions(attempts=3))
        self.agent = LlmAgent(
            name="uom_pack_reader", description="Reads the printed net quantity on a product pack.",
            model=model, instruction=INSTRUCTION, output_schema=PackReading, output_key="pack_reading",
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

    async def read(self, images: list[tuple[bytes, str]], product: str = "") -> PackReadResponse:
        t = self._types
        session_id = str(uuid4())
        started = perf_counter()
        await self.session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
        try:
            parts = []
            for number, (data, mime) in enumerate(images, 1):
                parts.append(t.Part(text=f"Photo {number}"))
                parts.append(t.Part(inline_data=t.Blob(mime_type=mime, data=data)))
            parts.append(t.Part(text=f"Product: {product or 'not given'}. What net quantity is printed on this pack?"))
            content = t.Content(role="user", parts=parts)
            final_text: str | None = None
            input_tokens = output_tokens = 0
            async with asyncio.timeout(self.timeout_seconds):
                async for event in self.runner.run_async(user_id=USER_ID, session_id=session_id, new_message=content):
                    usage = getattr(event, "usage_metadata", None)
                    if usage is not None:
                        input_tokens += getattr(usage, "prompt_token_count", 0) or 0
                        output_tokens += (getattr(usage, "candidates_token_count", 0) or 0) + (getattr(usage, "thoughts_token_count", 0) or 0)
                    if event.is_final_response() and event.content and event.content.parts:
                        final_text = "".join(part.text or "" for part in event.content.parts)
            if not final_text:
                raise RuntimeError("ADK run completed without a final structured response")
            result = PackReading.model_validate_json(final_text)
            return PackReadResponse(result, self.model_id, PACK_READER_VERSION, round((perf_counter() - started) * 1000), input_tokens, output_tokens)
        finally:
            try:
                await self.session_service.delete_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
            except Exception:  # noqa: BLE001
                logger.debug("pack reader session cleanup failed", exc_info=True)


def create_pack_reader(settings: Settings) -> Any:
    if settings.ai_provider.strip().lower() == "mock":
        return MockPackReader()
    return AdkPackReader(settings)
