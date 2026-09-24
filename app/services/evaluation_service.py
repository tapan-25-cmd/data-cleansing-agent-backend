from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any
from decimal import Decimal

import yaml

from app.domain.product import InputProduct
from app.rules.registry import load_default_registry
from app.services.excel_reader import FIELD_MAP, WorkbookRow
from app.services.group_a_validator import GroupAValidator
from app.services.result_ledger_service import enrich_result_item
from app.services.rule_engine import RuleEngine


CATALOG_PATH = (
    Path(__file__).resolve().parent.parent
    / "evaluations"
    / "uom_golden_cases.v1.yaml"
)


def _run_case(case: dict[str, Any]) -> dict[str, Any]:
    values = dict(case["input"])
    product = InputProduct(
        row_number=2,
        item_no=str(case["item_no"]),
        department="03_Grocery 2",
        raw_standard_size=Decimal(str(values["standard_size"])),
        raw_standard_uom=values.get("standard_uom"),
        raw_standard_pack_size=Decimal(str(values["standard_pack_size"])),
        **values,
    )
    row = WorkbookRow(
        row_number=2,
        product=product,
        raw={
            FIELD_MAP["standard_size"]: Decimal(str(values["standard_size"])),
            FIELD_MAP["standard_uom"]: values.get("standard_uom"),
            FIELD_MAP["standard_pack_size"]: Decimal(str(values["standard_pack_size"])),
        },
    )
    validation = GroupAValidator(
        RuleEngine(load_default_registry(), 0)
    ).validate(row)
    if validation is None:
        raise ValueError(f"evaluation case {case['id']} is not a complete Group A candidate")
    ledger = enrich_result_item({
        "route": "A",
        "original": {
            "standard_size": values.get("standard_size"),
            "standard_uom": values.get("standard_uom"),
            "standard_pack_size": values.get("standard_pack_size"),
        },
        "field_proposals": {
            "standard_size": None,
            "standard_uom": None,
            "standard_pack_size": None,
        },
        "field_provenance": {},
        "method": "NONE",
        "rule": {},
        "validation": validation.as_dict(),
        "review": {
            "field_decisions": {},
            "overall_status": "NOT_REQUIRED",
            "override_values": None,
            "comment": None,
        },
    })
    finding_codes = [finding["code"] for finding in ledger["findings"]]
    return {
        "application_policy": ledger["application_policy"],
        "finding_code": finding_codes[0] if finding_codes else None,
        "finding_codes": finding_codes,
        "proposed_standard_size": ledger["field_proposals"].get("standard_size"),
    }


def _matches_expected(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    for key, value in expected.items():
        if key == "finding_code":
            if value is None and actual["finding_codes"]:
                return False
            if value is not None and value not in actual["finding_codes"]:
                return False
        elif actual.get(key) != value:
            return False
    return True


@lru_cache(maxsize=1)
def load_evaluation_catalog() -> dict[str, Any]:
    payload = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError("evaluation catalog must contain a cases list")
    cases = []
    for source in payload["cases"]:
        actual = _run_case(source)
        cases.append({
            **source,
            "actual": actual,
            "passed": _matches_expected(source["expected"], actual),
        })
    approved = sum(case.get("label_status") == "APPROVED" for case in cases)
    proposed = sum(case.get("label_status") == "PROPOSED" for case in cases)
    approved_passed = sum(
        case.get("label_status") == "APPROVED" and case["passed"]
        for case in cases
    )
    proposed_passed = sum(
        case.get("label_status") == "PROPOSED" and case["passed"]
        for case in cases
    )
    deterministic_cases = sum(
        case.get("execution_scope", "DETERMINISTIC_RULE") == "DETERMINISTIC_RULE"
        for case in cases
    )
    agent_cases = sum(
        case.get("execution_scope") == "ADK_AGENT"
        for case in cases
    )
    business_decisions = [
        {
            "id": case["id"],
            "item_no": case["item_no"],
            "scenario": case["scenario"],
            "source_columns": case.get("source_columns", []),
            "decision": case.get("plain_language", {}).get("business_decision"),
        }
        for case in cases
        if case.get("plain_language", {}).get("business_decision")
    ]
    accuracy = round(approved_passed * 100 / approved, 1) if approved else None
    return {
        **payload,
        "cases": cases,
        "summary": {
            "total_cases": len(cases),
            "approved_labels": approved,
            "proposed_labels": proposed,
            "approved_passed": approved_passed,
            "proposed_behavior_matches": proposed_passed,
            "accuracy_available": approved > 0,
            "approved_accuracy_percent": accuracy,
            "accuracy_note": (
                f"{approved_passed} of {approved} business-approved regression cases pass "
                f"({accuracy}%). This is an early verified-case score, not production-wide accuracy."
            ),
            "coverage": {
                "deterministic_cases": deterministic_cases,
                "adk_agent_cases": agent_cases,
                "adk_agent_score_available": agent_cases > 0,
                "adk_agent_note": (
                    "No live ADK-agent evaluation cases are in this catalog yet. "
                    "The verified score on this page currently measures deterministic cleansing rules only."
                    if agent_cases == 0
                    else "ADK-agent cases are included separately from deterministic-rule cases."
                ),
            },
            "business_decisions": business_decisions,
        },
    }
