"""Run the reconcile task over chosen rows and score it. Read-only for the job."""
from __future__ import annotations

import asyncio
from collections import Counter
from functools import lru_cache
from pathlib import Path

import yaml
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from app.agents.reconcile import (
    DESCRIPTION_FIELDS, RECONCILE_PROMPT_VERSION, ReconcileRequest, ReconcileResponse, RuleOutcome, SourceValue,
)
from app.services.job_comparison_service import FIELDS, _values, describe, outcome, reviewer_verdict, load_reviewer_cases, _same
from app.services.result_status import STATUS_LABELS, effective_status, outcome_group, route_of

LANE_A_TRIAL_VERSION = "lane-a-trial-v2"
CASES_PATH = Path(__file__).resolve().parents[1] / "evaluations" / "reconcile_eval_cases.v1.yaml"
STANDARD_UOMS = {"GM", "ML", "EA"}


@lru_cache(maxsize=1)
def load_reconcile_cases() -> tuple[str, dict[str, dict[str, Any]]]:
    document = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8")) or {}
    cases = {}
    for case in document.get("cases") or []:
        expected = dict(case.get("expected") or {})
        values = expected.get("values")
        cases[str(case["item_no"])] = {
            "verdict": expected.get("verdict"),
            "values": _values(values["standard_size"], values["standard_uom"], values.get("standard_pack_size")) if values else None,
            "note": case.get("note"),
        }
    return str(document.get("version") or ""), cases


def category_kind(item: dict[str, Any], profile: dict[str, Any] | None) -> str:
    category = (item.get("context") or {}).get("category")
    if not profile or not category:
        return "UNKNOWN"
    if category in set(profile.get("liquid_categories") or []):
        return "LIQUID"
    if category in set(profile.get("mixed_categories") or []):
        return "MIXED"
    return "UNKNOWN"


def _decimal_or_none(value: object) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def _plain(value: object) -> str | None:
    if value in (None, ""):
        return None
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except (InvalidOperation, ValueError):
        return str(value)


def build_request(item: dict[str, Any], profile: dict[str, Any] | None = None) -> ReconcileRequest:
    context = item.get("context") or {}
    original = item.get("original") or {}
    result = outcome(item)
    excel = _values(*(original.get(f) for f in FIELDS))
    legacy = None
    if original.get("legacy_size") not in (None, "") and original.get("legacy_uom"):
        legacy = SourceValue(size=_plain(original.get("legacy_size")), uom=str(original.get("legacy_uom")).strip().upper())
    suggestion = result["suggestion"]
    return ReconcileRequest(
        descriptions={field: context.get(field) for field in DESCRIPTION_FIELDS},
        category=context.get("category") or item.get("category"),
        subcategory=context.get("subcategory") or item.get("subcategory"),
        category_kind=category_kind(item, profile),
        legacy=legacy,
        excel=SourceValue(size=excel["standard_size"], uom=excel["standard_uom"], pack_size=excel["standard_pack_size"], total=excel["total"]),
        rules=RuleOutcome(
            label=STATUS_LABELS.get(result["status"], result["status"]),
            comment=result["comment"][:1200],
            suggestion=SourceValue(size=suggestion["standard_size"], uom=suggestion["standard_uom"], pack_size=suggestion["standard_pack_size"], total=suggestion["total"]) if suggestion else None,
        ),
    )


def ai_values(item: dict[str, Any], response: ReconcileResponse) -> dict[str, str | None] | None:
    """The complete K/L/M the AI stands behind, or None for CANNOT_TELL."""
    r = response.result
    if r.verdict == "CANNOT_TELL" or r.proposed is None:
        return None
    original = item.get("original") or {}
    pack = r.proposed.standard_pack_size if r.proposed.standard_pack_size is not None else original.get("standard_pack_size")
    return _values(r.proposed.standard_size, r.proposed.standard_uom, pack)


def score_row(item: dict[str, Any], response: ReconcileResponse, reviewer_cases: dict[str, dict[str, Any]]) -> dict[str, Any]:
    result = outcome(item)
    values = ai_values(item, response)
    engine_final = result["values"]
    engine_suggestion = result["suggestion"]
    if values is None:
        agreement = "CANNOT_TELL"
    elif _same(values, engine_final) and engine_suggestion is None:
        agreement = "AGREES_WITH_ENGINE"
    elif engine_suggestion is not None and _same(values, engine_suggestion):
        agreement = "AGREES_WITH_SUGGESTION"
    elif _same(values, engine_final):
        agreement = "KEEPS_EXCEL_AGAINST_SUGGESTION"
    else:
        agreement = "DIFFERENT_ANSWER"
    reviewer = reviewer_cases.get(str(item.get("item_no")))
    reviewer_score = None
    if reviewer:
        expected = reviewer["expected"]
        if values is None:
            reviewer_score = "CANNOT_TELL"
        else:
            reviewer_score = "MATCHES_REVIEWER" if _same(values, expected) else "DIFFERS_FROM_REVIEWER"
    r = response.result
    _, cases = load_reconcile_cases()
    case = cases.get(str(item.get("item_no")))
    expected_score = None
    if case:
        if case["verdict"] == "CANNOT_TELL":
            expected_score = "MATCHES_EXPECTED" if values is None else "DIFFERS_FROM_EXPECTED"
        elif values is None:
            expected_score = "CANNOT_TELL"
        else:
            expected_score = "MATCHES_EXPECTED" if _same(values, case["values"]) else "DIFFERS_FROM_EXPECTED"
    guard_notes = []
    if values is not None:
        if values.get("standard_uom") not in STANDARD_UOMS:
            guard_notes.append("NON_STANDARD_UNIT")
        excel_total = _decimal_or_none(engine_final.get("total"))
        ai_total = _decimal_or_none(values.get("total"))
        if excel_total is not None and ai_total is not None and excel_total != ai_total and r.verdict not in {"DESCRIPTION_RIGHT", "LEGACY_RIGHT"}:
            guard_notes.append("TOTAL_CHANGED_WITHOUT_SOURCE")
    tier = (
        "CANNOT_TELL" if values is None
        else "APPLY_CANDIDATE" if r.confidence == "HIGH" and r.evidence and not r.needs_business_rule and not r.used_product_knowledge and not guard_notes
        else "SUGGEST"
    )
    return {
        "row_number": item.get("row_number"), "item_no": item.get("item_no"), "route": route_of(item), "group": outcome_group(item),
        "expected": {"verdict": case["verdict"], "values": case["values"], "score": expected_score, "note": case.get("note")} if case else None,
        "guards": guard_notes, "tier": tier,
        "status": result["status"], "engine_values": engine_final, "engine_suggestion": engine_suggestion,
        "engine_comment": result["comment"],
        "ai": {
            "verdict": r.verdict, "product_unit": r.product_unit, "values": values,
            "explanation": r.explanation, "confidence": r.confidence,
            "needs_business_rule": r.needs_business_rule, "business_rule_note": r.business_rule_note,
            "used_product_knowledge": r.used_product_knowledge,
            "evidence": [e.model_dump() for e in r.evidence], "sources": [s.model_dump() for s in r.sources],
        },
        "agreement": agreement,
        "reviewer": {"expected": reviewer["expected"], "answer": reviewer.get("answer"), "score": reviewer_score} if reviewer else None,
        "tokens": {"input": response.input_tokens, "output": response.output_tokens}, "latency_ms": response.latency_ms,
        "attempts": response.attempts,
    }


class LaneATrialService:
    def __init__(self, provider, *, max_concurrency: int = 5, category_profile: dict[str, Any] | None = None):
        self.provider = provider
        self.max_concurrency = max_concurrency
        self.category_profile = category_profile

    async def run_async(self, items: Iterable[dict[str, Any]]) -> dict[str, Any]:
        _, _, cases = load_reviewer_cases()
        reviewer_cases = {c["item_no"]: c for c in cases}
        semaphore = asyncio.Semaphore(self.max_concurrency)
        rows: list[dict[str, Any]] = []

        async def one(item: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                try:
                    response = await self.provider.reconcile(build_request(item, self.category_profile))
                except Exception as exc:  # noqa: BLE001
                    return {"row_number": item.get("row_number"), "item_no": item.get("item_no"), "status": effective_status(item),
                            "error": f"{type(exc).__name__}: {str(exc)[:300]}", "agreement": "FAILED", "reviewer": None, "ai": None}
                return score_row(item, response, reviewer_cases)

        rows = list(await asyncio.gather(*(one(item) for item in items)))
        rows.sort(key=lambda r: r.get("row_number") or 0)
        summary = {
            "version": LANE_A_TRIAL_VERSION, "prompt_version": RECONCILE_PROMPT_VERSION,
            "rows": len(rows),
            "agreement": dict(Counter(r["agreement"] for r in rows)),
            "reviewer": dict(Counter(r["reviewer"]["score"] for r in rows if r.get("reviewer"))),
            "confidence": dict(Counter(r["ai"]["confidence"] for r in rows if r.get("ai"))),
            "expected": dict(Counter(r["expected"]["score"] for r in rows if r.get("expected"))),
            "tiers": dict(Counter(r.get("tier") for r in rows if r.get("ai"))),
            "guards": dict(Counter(g for r in rows for g in r.get("guards") or [])),
            "wrong_and_confident": sum(1 for r in rows if r.get("expected") and r["expected"]["score"] == "DIFFERS_FROM_EXPECTED" and r["ai"]["confidence"] == "HIGH"),
            "failed": sum(1 for r in rows if r.get("agreement") == "FAILED"),
            "tokens": {"input": sum(r.get("tokens", {}).get("input", 0) for r in rows), "output": sum(r.get("tokens", {}).get("output", 0) for r in rows)},
        }
        return {"summary": summary, "rows": rows}

    def run(self, items: Iterable[dict[str, Any]]) -> dict[str, Any]:
        return asyncio.run(self.run_async(list(items)))
