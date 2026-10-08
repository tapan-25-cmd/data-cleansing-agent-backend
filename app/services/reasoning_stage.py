"""The reasoning layer as a pipeline stage.

Rules settle what is certain (arithmetic, equality, tolerance). Where a rule had to decide what
a number *means* — a size in the description that differs, a count beside the pack, a case
wording, an old size in another unit — the row is *triggered*, and the reasoning agent reads
every source and says what the numbers mean, quoting its evidence.

A deterministic gate then decides what that answer may do. In this phase the AI never writes
a new K/L/M value itself:

  CONFIRMED               the AI stands behind the value the row already holds, confidently,
                          with quoted evidence and no open business rule → the rule's raise is
                          cleared (a note records it)
  DISAGREES               the AI confidently gives a different answer → the row is raised with
                          the AI's values as the suggestion
  AGREES_WITH_SUGGESTION  the AI supports the rules' own suggestion → stays raised, noted
  SUGGESTS                a different answer without full confidence → attached as a suggestion
                          only when the row is already raised
  OPEN_DECISION           on a row the tool writes, the AI's answer (the same or different) rests
                          on an open business question → the row is raised for a person
  UNSUPPORTED             on a row the tool writes, the AI cannot tell → the written value goes
                          to a person
  CANNOT_TELL             nothing settles it → the row is unchanged; the explanation is kept
  UNAVAILABLE             the call failed → the row is unchanged

Modes: ``off`` (not run), ``shadow`` (recorded on the row, no effect), ``gate`` (the ledger
applies the outcomes above).
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from app.agents.reconcile import RECONCILE_PROMPT_VERSION, validate_evidence
from app.services.job_comparison_service import _same, outcome
from app.services.lane_a_trial_service import ai_values, build_request
from app.services.reasoning_codes import TRIGGER_CODES
from app.services.routes import route_of

logger = logging.getLogger(__name__)

REASONING_STAGE_VERSION = "reasoning-stage-v4"
MODES = ("off", "shadow", "gate")
STANDARD_UOMS = {"GM", "ML", "EA"}

# Rows the description reader already handled, or that cannot be read at all, are not sent.
_ROUTES = {"A", "VALIDATION_REVIEW", "B", "INCOMPLETE"}


def triggered(item: dict[str, Any]) -> bool:
    if route_of(item) not in _ROUTES:
        return False
    return bool({f.get("code") for f in item.get("findings") or []} & TRIGGER_CODES)


def _dec(value: object) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def tier(item: dict[str, Any], response: Any) -> tuple[str, list[str]]:
    """APPLY when the answer is confident, quotes the text, rests on no open business rule,
    uses a standard unit and changes no total without a source; SUGGEST otherwise; CANNOT_TELL
    without values. Product-kind knowledge is allowed (decided 23 September): knowing that
    eight abalone sit inside one jar, or that 3.3G in a yoghurt's name is protein, is what the
    reasoning layer is for. It is recorded on the row."""
    r = response.result
    values = ai_values(item, response)
    if values is None:
        return "CANNOT_TELL", []
    notes = []
    if values.get("standard_uom") not in STANDARD_UOMS:
        notes.append("NON_STANDARD_UNIT")
    before = _dec(outcome(item)["values"].get("total"))
    after = _dec(values.get("total"))
    if before is not None and after is not None and before != after and r.verdict not in {"DESCRIPTION_RIGHT", "LEGACY_RIGHT"}:
        notes.append("TOTAL_CHANGED_WITHOUT_SOURCE")
    apply = r.confidence == "HIGH" and bool(r.evidence) and not r.needs_business_rule and not notes
    return ("APPLY" if apply else "SUGGEST"), notes


def gate(item: dict[str, Any], response: Any) -> dict[str, Any]:
    """What the AI's answer may do to this row (see the module docstring)."""
    r = response.result
    result = outcome(item)
    values = ai_values(item, response)
    level, notes = tier(item, response)
    # A row the tool writes needs a second reading that supports it: an answer that rests on
    # an open business question, or that cannot tell, sends the written value to a person.
    written = result["status"] == "AUTO_APPLY"
    if values is None:
        decision = "UNSUPPORTED" if written else "CANNOT_TELL"
    elif written and r.needs_business_rule:
        decision = "OPEN_DECISION"
    elif _same(values, result["values"]):
        decision = "CONFIRMED" if level == "APPLY" else "AGREES"
    elif result["suggestion"] is not None and _same(values, result["suggestion"]):
        decision = "AGREES_WITH_SUGGESTION"
    else:
        decision = "DISAGREES" if level == "APPLY" else "SUGGESTS"
    return {
        "version": REASONING_STAGE_VERSION, "outcome": decision, "tier": level, "guards": notes,
        "verdict": r.verdict, "confidence": r.confidence, "values": values,
        "explanation": r.explanation, "evidence": [e.model_dump() for e in r.evidence],
        "needs_business_rule": r.needs_business_rule, "business_rule_note": r.business_rule_note,
        "used_product_knowledge": r.used_product_knowledge,
        "triggers": sorted({f.get("code") for f in item.get("findings") or []} & TRIGGER_CODES),
        "model_id": response.model_id, "prompt_version": response.prompt_version,
        "tokens": {"input": response.input_tokens, "output": response.output_tokens},
        "latency_ms": response.latency_ms, "attempts": response.attempts,
    }


class ReasoningStage:
    def __init__(self, provider: Any, mode: str = "shadow", *, max_rows: int = 3000,
                 max_concurrency: int = 5, category_profile: dict[str, Any] | None = None):
        if mode not in MODES:
            raise ValueError(f"reasoner mode must be one of {MODES}")
        self.provider = provider
        self.mode = mode
        self.max_rows = max_rows
        self.max_concurrency = max_concurrency
        self.category_profile = category_profile

    async def _run(self, rows: list[dict[str, Any]], on_done: Callable[[int], None]) -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(self.max_concurrency)
        done = 0
        out: list[dict[str, Any]] = [None] * len(rows)  # type: ignore[list-item]

        async def one(index: int, item: dict[str, Any]) -> None:
            nonlocal done
            try:
                request = build_request(item, self.category_profile)
                async with semaphore:
                    response = await self.provider.reconcile(request)
                validate_evidence(request, response.result)
                decision = gate(item, response)
            except Exception as exc:  # noqa: BLE001 - one failed row never stops the run
                logger.warning("reasoning failed for row %s: %s", item.get("row_number"), exc)
                decision = {"version": REASONING_STAGE_VERSION, "outcome": "UNAVAILABLE",
                            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                            "triggers": sorted({f.get("code") for f in item.get("findings") or []} & TRIGGER_CODES)}
            decision["mode"] = self.mode
            out[index] = decision
            done += 1
            on_done(done)

        await asyncio.gather(*(one(i, item) for i, item in enumerate(rows)))
        return out

    def run(self, enriched: list[dict[str, Any]], on_start: Callable[[int], None] = lambda n: None,
            on_done: Callable[[int], None] = lambda n: None) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
        """Reason over the triggered rows of ``enriched`` (ledger output). Returns the decision
        per index in ``enriched`` and the run's statistics."""
        if self.mode == "off" or self.provider is None:
            return {}, {"mode": self.mode, "triggered": 0, "reasoned": 0}
        indexes = [i for i, item in enumerate(enriched) if triggered(item)]
        capped = len(indexes) > self.max_rows
        chosen = indexes[: self.max_rows]
        on_start(len(chosen))
        decisions = asyncio.run(self._run([enriched[i] for i in chosen], on_done)) if chosen else []
        by_index = dict(zip(chosen, decisions))
        tokens = Counter()
        for d in decisions:
            for k, v in (d.get("tokens") or {}).items():
                tokens[k] += v
        stats = {
            "mode": self.mode, "version": REASONING_STAGE_VERSION, "prompt_version": RECONCILE_PROMPT_VERSION,
            "model_id": getattr(self.provider, "model_id", None),
            "triggered": len(indexes), "reasoned": len(chosen), "capped": capped,
            "outcomes": dict(Counter(d["outcome"] for d in decisions)),
            "tokens": dict(tokens),
        }
        return by_index, stats
