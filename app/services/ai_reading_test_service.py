"""On-request blind test of the AI's description reading.

Products that are already complete *and* state a size in their description are an
answer key for the AI. The agent is sent only the six permitted text fields (the
request schema forbids anything else), its observation is converted by the
deterministic rule engine exactly as in normal processing, and the result is compared
with the size the team entered.

Each product is one paid model call, so this never runs during normal processing. It is
started explicitly, and the model, prompt, and date are recorded with the result.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any

from app.agents.provider import InferenceProvider, InferenceRequest, validate_evidence
from app.repositories.mongo import MongoRepositories
from app.rules.registry import RuleRegistry
from app.agents.provider import PRODUCT_SIZE_ROLES
from app.services import guards
from app.services.category_profile import CategoryProfile, levels_of
from decimal import Decimal  # noqa: E402

from app.services.discrepancy_service import extract_field_signals
from app.services.quality_service import (
    DESCRIPTION_FIELDS,
    BlindInput,
    AI_READING_CAPABILITY,
    AI_READING_HOW,
    AI_READING_KEY,
    CapabilityScore,
    _decimal,
    _plain,
    answer_key,
    compare_size,
    is_ai_reading_eligible,
    text_states,
)
from app.services.rule_engine import RuleEngine

logger = logging.getLogger(__name__)
AI_READING_TEST_VERSION = "ai-reading-test-v3"
# A run that stopped reporting (for example after a server restart) may be replaced.
STALE_AFTER = timedelta(minutes=30)
_READY = frozenset({
    "READY_FOR_REVIEW", "REVIEW_IN_PROGRESS", "READY_TO_EXPORT", "EXPORTING", "EXPORTED",
})


class AiReadingTestBlocked(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sample(items: list[dict[str, Any]], limit: int | None) -> list[dict[str, Any]]:
    """Evenly spaced and repeatable, so a smaller sample still spans the workbook."""
    if not limit or limit >= len(items):
        return items
    step = len(items) / limit
    return [items[int(index * step)] for index in range(limit)]


class AiReadingTestService:
    def __init__(
        self,
        repositories: MongoRepositories,
        provider: InferenceProvider,
        registry: RuleRegistry,
        *,
        provider_is_real: bool,
        max_concurrency: int = 5,
        default_rounding_decimals: int | None = None,
    ):
        self.repositories = repositories
        self.provider = provider
        self.provider_is_real = provider_is_real
        self.max_concurrency = max_concurrency
        # Same engine configuration the pipeline uses for AI observations.
        self.rule_engine = RuleEngine(registry, default_rounding_decimals)

    def start(
        self, job_id: str, limit: int | None = None, *, only_unanswered: bool = False,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Validate and mark the run as started; returns (state, products to call).

        ``only_unanswered`` re-tests just the products whose last reading gave no
        answer or failed, and keeps every other stored reading. Nothing is billed
        here. ``run`` makes the model calls."""
        job = self.repositories.get_job(job_id)
        if not job:
            raise LookupError("job not found")
        if job.get("status") not in _READY:
            raise AiReadingTestBlocked("The workbook has not finished processing")
        if not self.provider_is_real:
            raise AiReadingTestBlocked(
                "The AI is switched off on this server (AI_PROVIDER=mock), so there is nothing to measure"
            )
        current = job.get("ai_reading_test") or {}
        if current.get("status") == "RUNNING":
            updated = current.get("updated_at")
            if isinstance(updated, datetime):
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                if _now() - updated < STALE_AFTER:
                    raise AiReadingTestBlocked("An AI reading test is already running")
        eligible = [
            item for item in self.repositories.quality_items(job_id)
            if is_ai_reading_eligible(item)
        ]
        if not eligible:
            raise AiReadingTestBlocked("No completed product states a size in its description")
        if only_unanswered:
            # Readings taken under different instructions must not be blended into one score.
            active = getattr(getattr(self.provider, "bundle", None), "prompt_version", None)
            if active and current.get("prompt_version") and active != current["prompt_version"]:
                raise AiReadingTestBlocked(
                    f"The AI's instructions changed ({current['prompt_version']} → {active}). "
                    "Run the full test so the score reflects one version."
                )
            retry = {
                record["row_number"] for record in self.repositories.ai_reading_results(job_id)
                if record.get("outcome") in {"NO_ANSWER", "ERROR"}
            }
            selected = [item for item in eligible if item.get("row_number") in retry]
            if not selected:
                raise AiReadingTestBlocked("Every tested product already has an answer")
        else:
            selected = _sample(eligible, limit)
        state = {
            "version": AI_READING_TEST_VERSION, "status": "RUNNING",
            "mode": "UNANSWERED" if only_unanswered else "ALL",
            "processed": 0, "total": len(selected), "available": len(eligible),
            "started_at": _now(), "updated_at": _now(), "finished_at": None,
            "errors": 0, "error": None,
            # The previous score stays visible until the new one replaces it.
            "score": current.get("score"),
        }
        self.repositories.update_job(job_id, {"ai_reading_test": state})
        return state, selected

    def run(
        self, job_id: str, selected: list[dict[str, Any]], *, only_unanswered: bool = False,
    ) -> None:
        try:
            asyncio.run(self._run(job_id, selected, only_unanswered))
        except Exception as exc:
            logger.exception("AI reading test failed for job %s", job_id)
            self.repositories.update_job(job_id, {
                "ai_reading_test.status": "FAILED",
                "ai_reading_test.error": f"{type(exc).__name__}: {exc}",
                "ai_reading_test.finished_at": _now(),
            })

    def score(
        self, records: list[dict[str, Any]], items: list[dict[str, Any]],
    ) -> tuple[CapabilityScore, int]:
        """Score stored AI readings with the *current* conversion rules.

        Pure and free: it makes no model call, so a rule change can be re-scored
        from the readings already paid for. Returns (score, failed calls)."""
        by_row = {item.get("row_number"): item for item in items}
        profile = CategoryProfile.from_items(items)
        score = CapabilityScore(AI_READING_KEY, AI_READING_CAPABILITY, AI_READING_HOW)
        errors = 0
        for record in sorted(records, key=lambda row: row.get("row_number") or 0):
            item = by_row.get(record.get("row_number"))
            if item is None:
                continue
            if record.get("error"):
                # A failed call says nothing about reading ability, so it is not scored.
                errors += 1
                record["outcome"] = "ERROR"
                continue
            answer_size, answer_uom, answer_pack = answer_key(item)
            excel = f"{_plain(answer_size)} {answer_uom}"
            observed = record.get("observed") or record.get("evidence")
            declined_because = None
            if record.get("status") == "PROPOSAL" and observed:
                given = f"“{observed['fragment']}”"
                role = observed.get("role")
                if role is not None and role not in PRODUCT_SIZE_ROLES:
                    # G1: the pipeline refuses this reading, so it counts as a decline.
                    declined_because = (
                        f"The AI read {given} but identified it as "
                        f"{guards.role_meaning(role)}, not the amount of product."
                    )
                else:
                    proposal = self.rule_engine.propose(observed["value"], observed["uom"])
                    if proposal is None:
                        record["outcome"] = "NO_ANSWER"
                        score.record(
                            item, False, "NO_ANSWER", given,
                            f"{_plain(observed['value'])} {observed['uom']}", excel,
                            f"The AI read {_plain(observed['value'])} {observed['uom']}, "
                            f"but {observed['uom']} has no agreed conversion.",
                        )
                        continue
                    # Shown to people rounded; compared at full precision.
                    agent = f"{_plain(round(proposal.standard_size, 1))} {proposal.standard_uom}"
                    record["agent"] = agent
                    if guards.implausible_size(
                        profile, levels_of(item), proposal.standard_size,
                        proposal.standard_uom, read_from_text=True,
                    ):
                        record["outcome"] = "SENT_TO_REVIEW"
                        # Stopping was right only if the reading really conflicts with Excel.
                        conflicts = not compare_size(
                            proposal.standard_size, proposal.standard_uom, proposal.factor != 1,
                            answer_size, answer_uom, given=given, unit_source="",
                        )[0]
                        score.record(
                            item, False, "SENT_TO_REVIEW", given, agent, excel,
                            f"The AI read {agent}, which is far outside the normal sizes for "
                            "this category, so the agent sends it to a person instead of using it."
                            + ("" if conflicts else " Here the value was in fact right, so asking was unnecessary."),
                            "CORRECT_CATCH" if conflicts else "AGENT_MISS",
                        )
                        continue
                    agrees, kind, note = compare_size(
                        proposal.standard_size, proposal.standard_uom, proposal.factor != 1,
                        answer_size, answer_uom,
                        given=given, unit_source=f"The {observed['uom']} the AI read",
                    )
                    pack = _decimal(record.get("pack_size"))
                    if (
                        not agrees and kind == "SIZE" and pack and answer_pack
                        and record.get("pack_role") in {None, "SELLABLE_PACK"}
                        and proposal.standard_size * pack == answer_size * answer_pack
                    ):
                        # D1: the same product split differently is not an error.
                        agrees, note = True, (
                            f"Same total. The AI read {agent} × {_plain(pack)}; your team "
                            f"entered {excel} × {_plain(answer_pack)}."
                        )
                    record["outcome"] = "AGREES" if agrees else kind
                    verdict = None
                    if not agrees:
                        verdict = self._why_it_differs(
                            item, record, proposal.standard_size, answer_size, answer_uom, answer_pack,
                        )
                        note += {
                            "RULE_APPLIED": " The agent follows the agreed rule: unit size is one piece.",
                            "DATA_PROBLEM": " The description states the agent's value and not Excel's.",
                        }.get(verdict, "")
                    score.record(item, agrees, kind, given, agent, excel, note, verdict)
                    continue
            reasons = {
                "NOT_IN_DESCRIPTION": "The AI did not find a product size in the description.",
                "AMBIGUOUS": "The AI found the wording too unclear to read a size safely.",
                "CONFLICT": "The AI found descriptions that state different sizes and did not pick one.",
            }
            reason = declined_because or reasons.get(str(record.get("status")), "The AI did not give a size.")
            if answer_uom == "EA" or not text_states(item, answer_size, answer_uom, answer_pack):
                # Nothing in the text states the true size (the team records a count, or the
                # number written is a grade or a name), so not reading one is the right answer.
                record["outcome"] = "AGREES"
                why = (
                    "Your team records this product as a count, so that is correct."
                    if answer_uom == "EA" else
                    f"The true size ({excel}) is not written in the text, so leaving it blank is correct."
                )
                score.record(
                    item, True, "CORRECT_DECLINE", "Description text only", "No size", excel,
                    f"{reason} {why}",
                )
            else:
                record["outcome"] = "NO_ANSWER"
                score.record(item, False, "NO_ANSWER", "Description text only", "No answer", excel, reason)
        return score, errors

    @staticmethod
    def _why_it_differs(item: dict[str, Any], record: dict[str, Any], agent_size: Decimal,
                        answer_size: Decimal | None, answer_uom: str, answer_pack: Decimal | None) -> str:
        """The AI read its value from the text, so the text always backs the agent.
        What matters is whether the text backs Excel too, or D1 explains the difference."""
        # D1: a bundle the team recorded as a count ("500ML x 2" entered as 1 EA x 2).
        if answer_uom == "EA":
            return "RULE_APPLIED"
        # D1: Excel holds a multiple of the piece size that the text itself spells out
        # ("5 CASE/6 X 90GM" entered as 450 GM).
        if answer_size and agent_size and answer_size % agent_size == 0:
            multiple = int(answer_size / agent_size)
            blind = BlindInput.from_item(item)
            counts = {
                count.value
                for field, attribute in DESCRIPTION_FIELDS
                for count in extract_field_signals(field, getattr(blind, attribute)).counts
            }
            if multiple > 1 and multiple in counts:
                return "RULE_APPLIED"
        if text_states(item, answer_size, answer_uom, answer_pack):
            return "NEEDS_DECISION"  # the text supports both values
        return "DATA_PROBLEM"

    def rescore(self, job_id: str) -> dict[str, Any]:
        """Re-score the stored readings after a rule change. Makes no model call."""
        job = self.repositories.get_job(job_id)
        if not job:
            raise LookupError("job not found")
        records = self.repositories.ai_reading_results(job_id)
        if not records or (job.get("ai_reading_test") or {}).get("status") == "RUNNING":
            raise AiReadingTestBlocked("There is no finished AI reading test to re-score")
        score, errors = self.score(records, self.repositories.quality_items(job_id))
        self.repositories.replace_ai_reading_results(job_id, records)
        self.repositories.update_job(job_id, {
            "ai_reading_test.score": score.state(),
            "ai_reading_test.errors": errors,
            "ai_reading_test.rescored_at": _now(),
            "quality": None,
        })
        return score.state()

    async def _run(
        self, job_id: str, selected: list[dict[str, Any]], only_unanswered: bool,
    ) -> None:
        semaphore = asyncio.Semaphore(self.max_concurrency)
        records: list[dict[str, Any]] = []
        metadata: dict[str, Any] = {}
        done = failed = 0
        last_write = monotonic()

        async def test(item: dict[str, Any]) -> None:
            nonlocal done, failed, last_write
            context = item.get("context") or {}
            # The only data the AI sees. InferenceRequest forbids any other field,
            # so the entered size, unit, pack size and legacy values cannot leak.
            request = InferenceRequest(
                item_brand_eng=context.get("item_brand_eng"),
                item_brand_local_lang=context.get("item_brand_local_lang"),
                item_desc_eng=context.get("item_desc_eng"),
                item_desc_local_lang=context.get("item_desc_local_lang"),
                web_description_eng=context.get("web_description_eng"),
                web_description_chi=context.get("web_description_chi"),
                # D3: category context, exactly as in normal processing.
                division=context.get("division"),
                category=context.get("category"),
                subcategory=context.get("subcategory"),
                section=context.get("section"),
            )
            record: dict[str, Any] = {
                "job_id": job_id, "row_number": item.get("row_number"),
                "item_no": item.get("item_no"), "tested_at": _now(),
            }
            try:
                async with semaphore:
                    response = await self.provider.infer(request)
                result = response.result
                validate_evidence(request, result)
                metadata.update(response.metadata.model_dump(mode="json"))
                # The raw reading is kept so it can be re-scored without a new call.
                record.update(
                    status=result.status,
                    observed=result.measurement.model_dump(mode="json") if result.measurement else None,
                    other_measurements=[m.model_dump(mode="json") for m in result.other_measurements],
                    pack_size=str(result.pack_size) if result.pack_size is not None else None,
                    pack_role=result.pack_role,
                    rationale=result.rationale,
                    input_tokens=response.metadata.input_tokens,
                    output_tokens=response.metadata.output_tokens,
                    latency_ms=response.metadata.latency_ms,
                    model_id=response.metadata.model_id,
                    prompt_version=response.metadata.prompt_version,
                )
            except Exception as exc:
                failed += 1
                record["error"] = f"{type(exc).__name__}: {exc}"
            records.append(record)
            done += 1
            if done == len(selected) or monotonic() - last_write >= 1:
                last_write = monotonic()
                self.repositories.update_job(job_id, {
                    "ai_reading_test.processed": done,
                    "ai_reading_test.errors": failed,
                    "ai_reading_test.updated_at": _now(),
                })

        await asyncio.gather(*(test(item) for item in selected))

        if only_unanswered:
            retested = {record["row_number"] for record in records}
            records = [
                record for record in self.repositories.ai_reading_results(job_id)
                if record.get("row_number") not in retested
            ] + records
        score, errors = self.score(records, self.repositories.quality_items(job_id))
        self.repositories.replace_ai_reading_results(job_id, records)
        updates = {
            "ai_reading_test.status": "COMPLETED",
            "ai_reading_test.processed": done,
            "ai_reading_test.errors": errors,
            "ai_reading_test.finished_at": _now(),
            "ai_reading_test.updated_at": _now(),
            "ai_reading_test.score": score.state(),
            # The cached report no longer reflects this job.
            "quality": None,
        }
        for key in ("model_id", "prompt_version", "prompt_sha256", "agent_version", "provider"):
            if metadata.get(key):
                updates[f"ai_reading_test.{key}"] = metadata[key]
        # One line per completed run, so versions of the agent can be compared over time.
        job = self.repositories.get_job(job_id) or {}
        updates["ai_reading_history"] = [*(job.get("ai_reading_history") or []), {
            "finished_at": _now(), "mode": "UNANSWERED" if only_unanswered else "ALL",
            "prompt_version": metadata.get("prompt_version"), "model_id": metadata.get("model_id"),
            "agent_version": metadata.get("agent_version"),
            "tested": score.tested, "same_answer": score.agreed, "no_answer": score.no_answer,
            "differs": sum(score.disagreements.values()), "failed_calls": errors,
            "accuracy_percent": round(score.agreed * 100 / score.tested, 1) if score.tested else None,
            "input_tokens": sum(record.get("input_tokens") or 0 for record in records),
            "output_tokens": sum(record.get("output_tokens") or 0 for record in records),
        }][-20:]
        self.repositories.update_job(job_id, updates)
        logger.info(json.dumps({
            "event": "ai_reading_test.completed", "job_id": job_id, "tested": score.tested,
            "agreed": score.agreed, "no_answer": score.no_answer, "errors": errors,
        }))
