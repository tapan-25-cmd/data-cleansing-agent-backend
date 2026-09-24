"""Everything the engine uses to decide, assembled for a non-technical reader."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.agents.versions import AGENT_VERSION, PROMPT_VERSION
from app.rules.registry import RuleRegistry
from app.services import discrepancy_service as text
from app.services.export_service import EXPORT_VERSION
from app.services.group_a_validator import GROUP_A_VALIDATION_VERSION
from app.services.guards import GUARDS_VERSION
from app.services.klm_reconciliation_service import KLM_RECONCILIATION_VERSION
from app.services.quality_service import load_business_decisions
from app.services.result_ledger_service import RESULT_LEDGER_VERSION
from app.services.result_status import STATUS_LABELS

RULES_GUIDE_PATH = Path(__file__).resolve().parents[1] / "evaluations" / "rules_guide.v1.yaml"


@lru_cache(maxsize=1)
def load_guide() -> dict[str, Any]:
    return yaml.safe_load(RULES_GUIDE_PATH.read_text(encoding="utf-8")) or {}


def _plain(value: Any) -> str:
    try:
        return format(value.normalize(), "f")
    except AttributeError:
        return str(value)


def unit_mapping(registry: RuleRegistry) -> list[dict[str, Any]]:
    rows = []
    for rule in registry.ruleset.rules:
        factor = rule.factor
        example_source = "1" if factor >= 1 else "100"
        example_value = factor if factor >= 1 else factor * 100
        rows.append({
            "rule_id": rule.rule_id,
            "source_uoms": list(rule.source_uoms),
            "target_uom": rule.target_uom,
            "factor": _plain(factor),
            "example": f"{example_source} {rule.source_uoms[0]} becomes {_plain(example_value)} {rule.target_uom}",
            "enabled": rule.enabled,
            "notes": rule.notes,
        })
    return rows


def description_reading() -> dict[str, Any]:
    units = [
        {"written": written, "means": meaning[0], "counted_as": meaning[1], "kind": meaning[2].title()}
        for written, meaning in text._UNITS.items()
    ]
    return {
        "units": units,
        "pack_words": ["'S", "PIECES", "PCS", "PACKS", "PKS", "CUPS", "CANS", "BOTTLES", "SACHETS", "STICKS", "BAGS", "CT",
                       "包裝", "孖裝", "件裝", "支裝", "杯裝", "粒裝", "罐裝", "原箱", "CASE"],
        "count_notations": [
            {"pattern": "N x size", "example": "4 x 200ML, 500MLX2", "meaning": "N pieces of that size"},
            {"pattern": "N'S / NS / NP", "example": "4'S, 10S, 4P", "meaning": "a pack of N"},
            {"pattern": "\\N", "example": "NDL\\10", "meaning": "N packs, used only when the arithmetic closes"},
            {"pattern": "N CASE / CS", "example": "5 CASE/10X90GM", "meaning": "an outer case of N"},
            {"pattern": "Chinese numeral + classifier", "example": "三件裝, 孖裝, 6枝, 50包裝", "meaning": "a count in Chinese"},
        ],
        "classifiers": text._CJK_CLASSIFIER.split("|"),
        "numerals": {k: v for k, v in text._CJK_NUMERALS.items()},
        "pairs": [
            {"pair": "Item description", "english": "item_desc_eng", "local": "item_desc_local_lang"},
            {"pair": "Web description", "english": "web_description_eng", "local": "web_description_chi"},
        ],
        "brand_note": "Brand fields are context only; a number in a brand is never a size or a count.",
        "not_a_size": load_guide().get("not_a_size", []),
    }


def build_guide(registry: RuleRegistry, job: dict[str, Any] | None) -> dict[str, Any]:
    guide = load_guide()
    job = job or {}
    policy = job.get("validation_policy") or {}
    decisions_version, decisions = load_business_decisions()
    business = [
        {"id": d.id, "title": d.title, "status": getattr(d, "status", "OPEN"),
         "decided_note": getattr(d, "decided_note", None)}
        for d in decisions
    ]
    return {
        "version": guide.get("version"),
        "flow": guide.get("flow", []),
        "labels": STATUS_LABELS,
        "unit_mapping": {
            "ruleset_version": registry.version,
            "rounding": "Nearest whole number, the way Excel rounds",
            "rules": unit_mapping(registry),
            "readiness": job.get("rule_readiness"),
        },
        "legacy_checks": guide.get("legacy_checks", []),
        "description_reading": description_reading(),
        "ai_rules": guide.get("ai_rules", []),
        "guards": guide.get("guards", []),
        "category_profile": policy.get("category_profile"),
        "pipeline": guide.get("pipeline", {}),
        "reasoning": guide.get("reasoning", {}),
        "accuracy_method": guide.get("accuracy_method", {}),
        "decisions": guide.get("decisions", []),
        "business_decisions": {"version": decisions_version, "items": business},
        "versions": {
            "unit_mapping": registry.version,
            "validation": GROUP_A_VALIDATION_VERSION,
            "legacy_reconciliation": KLM_RECONCILIATION_VERSION,
            "safety_checks": GUARDS_VERSION,
            "result_ledger": RESULT_LEDGER_VERSION,
            "export": EXPORT_VERSION,
            "agent": AGENT_VERSION,
            "prompt": PROMPT_VERSION,
            "this_job": {
                "validation": policy.get("version"),
                "safety_checks": policy.get("guards_version"),
                "unit_mapping": job.get("ruleset_version"),
                "prompt": (job.get("ai_usage") or {}).get("prompt_version"),
            } if job else None,
        },
    }
