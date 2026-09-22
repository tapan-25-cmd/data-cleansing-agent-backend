"""Evaluation gate for the AI agent: a prompt or model change is measured, not assumed.

Each case is a real product text with the outcome the agent must reach. A case is run
through the same steps as production (evidence validation, role gate, pack-role gate,
rule engine), so the score reflects what would actually be written, not what the model
said. The number that must be zero is **wrong and confident**: a size applied that
should not have been.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.agents.provider import InferenceProvider, InferenceRequest, validate_evidence
from app.rules.registry import RuleRegistry
from app.services import guards
from app.services.discrepancy_service import within_conversion_tolerance
from app.services.rule_engine import RuleEngine

CASES_PATH = Path(__file__).resolve().parent.parent / "evaluations" / "agent_eval_cases.v1.yaml"


@lru_cache(maxsize=1)
def load_cases() -> tuple[str, tuple[dict[str, Any], ...]]:
    payload = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
    ids = [case["id"] for case in payload["cases"]]
    if len(set(ids)) != len(ids):
        raise ValueError("evaluation case ids must be unique")
    return str(payload["version"]), tuple(payload["cases"])


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


class AgentEvaluationService:
    def __init__(self, provider: InferenceProvider, registry: RuleRegistry,
                 *, max_concurrency: int = 5, default_rounding_decimals: int | None = 0):
        self.provider = provider
        self.rule_engine = RuleEngine(registry, default_rounding_decimals)
        self.max_concurrency = max_concurrency

    async def _answer(self, case: dict[str, Any]) -> dict[str, Any]:
        """What production would apply for this text: a size, a pack, or nothing."""
        request = InferenceRequest(category=case.get("category"), **case["text"])
        response = await self.provider.infer(request)
        result = response.result
        validate_evidence(request, result)
        size = None
        if result.status == "PROPOSAL" and result.measurement and result.measurement.describes_product_size:
            proposal = self.rule_engine.propose(result.measurement.value, result.measurement.uom)
            if proposal is not None:
                size = (proposal.standard_size, proposal.standard_uom)
        pack = (
            int(result.pack_size) if result.pack_size is not None and guards.is_sellable_pack(result)
            else None
        )
        return {
            "size": size, "pack": pack, "status": result.status, "rationale": result.rationale,
            "prompt_version": response.metadata.prompt_version, "model_id": response.metadata.model_id,
            "tokens": (response.metadata.input_tokens or 0) + (response.metadata.output_tokens or 0),
        }

    @staticmethod
    def _judge(expect: dict[str, Any], answer: dict[str, Any]) -> tuple[bool, bool]:
        """(passed, wrong_and_confident)."""
        wanted = expect["size"]
        got = answer["size"]
        if wanted == "DECLINE":
            size_ok = got is None
        else:
            value, unit = wanted.split()
            size_ok = got is not None and got[1] == unit and within_conversion_tolerance(got[0], Decimal(value))
        pack_ok = answer["pack"] == expect.get("pack")
        return size_ok and pack_ok, (got is not None and not size_ok) or (
            answer["pack"] is not None and not pack_ok
        )

    async def run_async(self) -> dict[str, Any]:
        version, cases = load_cases()
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def one(case: dict[str, Any]) -> dict[str, Any]:
            row = {
                "id": case["id"], "pattern": case["pattern"], "item_no": case.get("item_no"),
                "text": " / ".join(str(value) for value in case["text"].values()),
                "expected": self._describe(case["expect"]["size"], case["expect"].get("pack")),
            }
            try:
                async with semaphore:
                    answer = await self._answer(case)
            except Exception as exc:  # a failed call is reported, never scored as right
                return {**row, "answer": f"Call failed: {type(exc).__name__}", "passed": False,
                        "wrong_and_confident": False, "failed_call": True, "rationale": None}
            passed, wrong = self._judge(case["expect"], answer)
            size = "DECLINE" if answer["size"] is None else f"{_plain(answer['size'][0])} {answer['size'][1]}"
            return {**row, "answer": self._describe(size, answer["pack"]), "passed": passed,
                    "wrong_and_confident": wrong, "failed_call": False,
                    "rationale": answer["rationale"], "_meta": answer}

        rows = await asyncio.gather(*(one(case) for case in cases))
        patterns: dict[str, dict[str, int]] = defaultdict(lambda: {"cases": 0, "passed": 0, "wrong_and_confident": 0})
        for row in rows:
            bucket = patterns[row["pattern"]]
            bucket["cases"] += 1
            bucket["passed"] += row["passed"]
            bucket["wrong_and_confident"] += row["wrong_and_confident"]
        meta = next((row["_meta"] for row in rows if row.get("_meta")), {})
        tokens = sum((row.get("_meta") or {}).get("tokens", 0) for row in rows)
        for row in rows:
            row.pop("_meta", None)
        passed = sum(row["passed"] for row in rows)
        return {
            "cases_version": version,
            "prompt_version": meta.get("prompt_version"), "model_id": meta.get("model_id"),
            "cases": len(rows), "passed": passed,
            "pass_percent": round(passed * 100 / len(rows), 1) if rows else None,
            "wrong_and_confident": sum(row["wrong_and_confident"] for row in rows),
            "failed_calls": sum(row["failed_call"] for row in rows),
            "tokens": tokens,
            "patterns": [{"pattern": name, **values} for name, values in sorted(patterns.items())],
            "rows": rows,
        }

    def run(self) -> dict[str, Any]:
        return asyncio.run(self.run_async())

    @staticmethod
    def _describe(size: str, pack: int | None) -> str:
        text = "No size applied" if size == "DECLINE" else size
        return f"{text} · pack {pack}" if pack is not None else text
