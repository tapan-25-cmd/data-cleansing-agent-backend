from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.services.guards import GUARD_REVIEW_CODES


RESULT_LEDGER_VERSION = "result-ledger-v4"
AUDIT_FIELDS = ("standard_size", "standard_uom", "standard_pack_size")

_FINDING_METADATA = {
    "ROUNDING_ONLY_VARIANCE": (
        "ROUNDING",
        "INFO",
        "Difference is within conversion rounding tolerance",
    ),
    "SIGNIFICANT_LEGACY_SIZE_MISMATCH": (
        "SOURCE_DISCREPANCY",
        "REVIEW",
        "Standardized size differs materially from legacy conversion",
    ),
    "LEGACY_NOT_COMPARABLE": (
        "VALIDATION",
        "INFO",
        "Could not be double-checked against the legacy data",
    ),
    "LINKED_SIZE_AND_PACK_SUGGESTION": (
        "SOURCE_DISCREPANCY",
        "REVIEW",
        "Legacy size and description count together match Excel's total; suggested as one change",
    ),
    "DESCRIPTION_PACK_COUNT_DIFFERS": (
        "SOURCE_DISCREPANCY",
        "REVIEW",
        "The description's count differs from Excel's pack size",
    ),
    "DESCRIPTION_CONFIRMS_PIECE_COUNT": (
        "SOURCE_AGREEMENT",
        "INFO",
        "The description confirms the piece count Excel already has",
    ),
    "DESCRIPTION_CONFIRMS_UNIT_SIZE": (
        "SOURCE_AGREEMENT",
        "INFO",
        "The description confirms the size Excel already has",
    ),
    "LEGACY_TOTAL_CONSISTENT": (
        "PACKAGING_HIERARCHY",
        "INFO",
        "Legacy value agrees with the existing whole-pack total",
    ),
    "LEGACY_UOM_MISMATCH": (
        "SOURCE_DISCREPANCY",
        "WARNING",
        "Legacy conversion targets a different unit",
    ),
    "PACKAGING_HIERARCHY_AMBIGUOUS": (
        "PACKAGING_HIERARCHY",
        "REVIEW",
        "Package expression does not support existing K/L/M",
    ),
    "BILINGUAL_DESCRIPTION_CONFLICT": (
        "DESCRIPTION_CONFLICT",
        "REVIEW",
        "Description fields contain conflicting measurements",
    ),
    "PACK_COUNT_CONFLICT": (
        "DESCRIPTION_CONFLICT",
        "REVIEW",
        "English and local-language text state different pack counts",
    ),
    "PACKAGING_LEVEL_DIFFERENCE": (
        "PACKAGING_HIERARCHY",
        "INFO",
        "Descriptions state the same product at different packaging levels",
    ),
    "FLUID_OUNCE_AMBIGUOUS": (
        "DESCRIPTION_CONFLICT",
        "INFO",
        "An OZ value may be a fluid ounce, so weight and volume were not compared",
    ),
    "OUNCE_MAY_BE_FLUID": (
        "UNIT_AMBIGUITY",
        "REVIEW",
        "OZ could be a weight or a fluid ounce",
    ),
    "OUNCE_READ_AS_FLUID": (
        "UNIT_AMBIGUITY",
        "INFO",
        "OZ was converted as fluid ounces",
    ),
    "TEXT_CONTRADICTS_RESULT": (
        "DESCRIPTION_CONFLICT",
        "REVIEW",
        "The description does not fit the converted value",
    ),
    "SIZE_OUTSIDE_CATEGORY_RANGE": (
        "PLAUSIBILITY",
        "REVIEW",
        "The size is far outside what is normal for this category",
    ),
    "LEGACY_MAY_BE_PACK_TOTAL": (
        "PACKAGING_HIERARCHY",
        "REVIEW",
        "The legacy size looks like the whole pack, not one piece",
    ),
    "AI_PACK_NEEDS_CONFIRMATION": (
        "PACK_EVIDENCE",
        "REVIEW",
        "The AI read a pack size that a person should confirm",
    ),
    "SIZE_UNUSUAL_FOR_CATEGORY": (
        "PLAUSIBILITY",
        "INFO",
        "The size is unusual for this category",
    ),
    "AI_MEASUREMENT_NOT_PRODUCT_SIZE": (
        "PLAUSIBILITY",
        "REVIEW",
        "The number the AI read does not describe the amount of product",
    ),
    "COUNT_IN_MEASURED_CATEGORY": (
        "INSUFFICIENT_EVIDENCE",
        "INFO",
        "Only a count is available for this product",
    ),
    "DESCRIPTION_MEASUREMENT_MISMATCH": (
        "DESCRIPTION_CONFLICT",
        "WARNING",
        "Description measurement differs from existing K/L",
    ),
    "AGENT_NO_EXPLICIT_EVIDENCE": (
        "INSUFFICIENT_EVIDENCE",
        "INFO",
        "Agent found no explicit size or UOM",
    ),
    "UNMAPPED_SOURCE_UOM": (
        "INSUFFICIENT_EVIDENCE",
        "INFO",
        "The legacy unit has no agreed conversion",
    ),
    "AGENT_PROCESSING_ERROR": (
        "AGENT_PROCESSING",
        "ERROR",
        "Agent could not complete the description check",
    ),
}

REVIEW_CODES = GUARD_REVIEW_CODES | frozenset({
    "SIGNIFICANT_LEGACY_SIZE_MISMATCH",
    "LINKED_SIZE_AND_PACK_SUGGESTION",
    "DESCRIPTION_PACK_COUNT_DIFFERS",
    "LEGACY_UOM_MISMATCH",
    "PACKAGING_HIERARCHY_AMBIGUOUS",
    "BILINGUAL_DESCRIPTION_CONFLICT",
    "PACK_COUNT_CONFLICT",
})

_PAIR_SIDES = {
    "brand": ("Brand (English)", "Brand (local language)"),
    "item_desc": ("Item description (English)", "Item description (local language)"),
    "web_desc": ("Web description (English)", "Web description (local language)"),
}
_DISCREPANCY_CODES = {
    ("BILINGUAL_PAIR", "MEASUREMENT"): "BILINGUAL_DESCRIPTION_CONFLICT",
    ("BILINGUAL_PAIR", "COUNT"): "PACK_COUNT_CONFLICT",
}


def _stated(signals: list[dict[str, Any]]) -> str:
    fragments = list(dict.fromkeys(str(signal.get("fragment") or "").strip() for signal in signals))
    return ", ".join(f"“{fragment}”" for fragment in fragments if fragment)


def _discrepancy_findings(
    item: dict[str, Any],
    existing_codes: set[str],
) -> list[dict[str, Any]]:
    """Turn discrepancy-engine conflicts and observations into ledger findings.

    Missing-side results (INSUFFICIENT_COMPARISON_DATA) stay in the discrepancy
    record only; they are not a finding a reviewer can act on.
    """
    findings: list[dict[str, Any]] = []
    for detail in (item.get("discrepancy") or {}).get("details") or []:
        status = detail.get("status")
        if status == "CONFLICT":
            code = _DISCREPANCY_CODES[(detail["scope"], detail["aspect"])]
        elif status == "OBSERVATION":
            code = str(detail["classification"])
        else:
            continue
        # Group A validation already reports bilingual measurement conflicts.
        if code == "BILINGUAL_DESCRIPTION_CONFLICT" and code in existing_codes:
            continue
        left_label, right_label = _PAIR_SIDES.get(str(detail.get("pair")), ("First source", "Second source"))
        left = _stated(detail.get("left_signals") or [])
        right = _stated(detail.get("right_signals") or [])
        subject = "pack count" if detail.get("aspect") == "COUNT" else "size"
        category, severity, title = _FINDING_METADATA[code]
        if status == "CONFLICT":
            reason = (
                f"{left_label} states {left}, but {right_label.lower()} states {right}. "
                f"The {subject} cannot be confirmed from the text, so no value was chosen automatically."
            )
            if detail.get("classification") == "LIKELY_TYPO":
                reason += " The two values look like a possible typing error."
        else:
            reason = f"{left_label} states {left}; {right_label.lower()} states {right}. {title}."
        findings.append({
            "code": code,
            "category": category,
            "severity": severity,
            "title": title,
            "human_reason": reason,
            "field": detail.get("pair"),
            "classification": detail.get("classification"),
            "evidence": [
                {"role": "LEFT", "label": left_label, "value": left},
                {"role": "RIGHT", "label": right_label, "value": right},
            ],
            "policy_version": RESULT_LEDGER_VERSION,
        })
    return findings


def _finding(issue: dict[str, Any]) -> dict[str, Any]:
    code = str(issue.get("code") or "VALIDATION_OBSERVATION")
    category, severity, title = _FINDING_METADATA.get(
        code,
        ("VALIDATION", str(issue.get("severity") or "INFO"), code.replace("_", " ").title()),
    )
    evidence = []
    current = issue.get("current_value")
    expected = issue.get("expected_value")
    if current is not None:
        evidence.append({"role": "CURRENT", "value": current})
    if expected is not None:
        evidence.append({"role": "EXPECTED", "value": expected})
    return {
        "code": code,
        "proposed": dict(issue["proposed"]) if issue.get("proposed") else None,
        "category": category,
        "severity": severity,
        "title": title,
        "human_reason": str(issue.get("message") or title),
        "field": issue.get("field"),
        "evidence": evidence,
        "policy_version": RESULT_LEDGER_VERSION,
    }


def enrich_result_item(source: dict[str, Any]) -> dict[str, Any]:
    """Add stable, UI/export-ready findings and field-level change records."""
    item = deepcopy(source)
    validation = item.get("validation") or {}
    # Guards use the same issue shape as validation, so both become findings.
    issues = list(validation.get("issues") or []) + list(item.get("guards") or [])
    findings = [_finding(issue) for issue in issues]
    issue_codes = {str(issue.get("code")) for issue in issues}
    discrepancy_findings = _discrepancy_findings(item, issue_codes)
    findings.extend(discrepancy_findings)
    issue_codes |= {finding["code"] for finding in discrepancy_findings}
    proposals = item.setdefault("field_proposals", {})
    original = item.get("original") or {}

    # A material legacy mismatch is a proposal for a human, never an automatic
    # overwrite. The expected value was produced by the versioned rule engine.
    mismatch = next((
        issue for issue in issues
        if issue.get("code") == "SIGNIFICANT_LEGACY_SIZE_MISMATCH"
        and issue.get("expected_value") is not None
    ), None)
    linked = next((
        issue for issue in issues
        if issue.get("code") == "LINKED_SIZE_AND_PACK_SUGGESTION" and issue.get("proposed")
    ), None)
    if linked and proposals.get("standard_size") is None:
        # Size and pack are one suggestion: approving one without the other would
        # change the total, which is exactly what this suggestion preserves.
        for field, value in dict(linked["proposed"]).items():
            proposals[field] = value
            item.setdefault("field_provenance", {})[field] = {
                "method": "RULE",
                "rule_id": "LEGACY_LINKED_PACK",
                "policy_version": RESULT_LEDGER_VERSION,
            }
    elif mismatch and proposals.get("standard_size") is None:
        proposals["standard_size"] = mismatch["expected_value"]
        if original.get("standard_uom") is not None:
            proposals["standard_uom"] = original["standard_uom"]
        item.setdefault("field_provenance", {})["standard_size"] = {
            "method": "RULE",
            "rule_id": "LEGACY_COMPARISON",
            "policy_version": RESULT_LEDGER_VERSION,
        }

    has_proposal = any(proposals.get(field) is not None for field in AUDIT_FIELDS)
    requires_review = bool(issue_codes & REVIEW_CODES)
    no_size = proposals.get("standard_size") is None and proposals.get("standard_uom") is None
    rule_unresolved = (
        item.get("group") == "B" and no_size
        and item.get("reason_code") in {"NO_RULE", "MALFORMED_VALUE"}
    )
    agent_unresolved = item.get("group") == "C" and not has_proposal
    if rule_unresolved and not has_proposal:
        legacy_uom = str(original.get("legacy_uom") or "this unit")
        findings.append(_finding({
            "code": "UNMAPPED_SOURCE_UOM",
            "field": "legacy_uom",
            "severity": "INFO",
            "message": (
                f"The legacy unit {legacy_uom} has no agreed conversion, so the size could "
                "not be filled in. It was left blank rather than guessed."
            ),
            "current_value": legacy_uom,
        }))
        policy = "UNRESOLVED"
    elif agent_unresolved:
        error = str(item.get("reason_code") or "") in {
            "AI_PROVIDER_ERROR", "AI_INVALID_RESPONSE"
        }
        findings.append(_finding({
            "code": "AGENT_PROCESSING_ERROR" if error else "AGENT_NO_EXPLICIT_EVIDENCE",
            "field": "descriptions",
            "severity": "ERROR" if error else "INFO",
            "message": (
                "The agent could not complete its description analysis. No K/L/M value was changed."
                if error else
                "The agent checked every permitted description field but found no explicit size and unit. It abstained instead of guessing."
            ),
        }))
        policy = "UNRESOLVED"
    elif requires_review:
        policy = "REVIEW_REQUIRED"
        review = item.setdefault("review", {})
        decisions = review.setdefault("field_decisions", {})
        for field in AUDIT_FIELDS:
            if proposals.get(field) is not None:
                decisions[field] = "PENDING"
            else:
                decisions.setdefault(field, "NOT_REQUIRED")
        review["overall_status"] = "PENDING"
        review.setdefault("override_values", None)
        review.setdefault("comment", None)
    elif has_proposal:
        policy = "AUTO_APPLY"
    elif findings:
        policy = "OBSERVATION_ONLY"
    else:
        policy = "NO_CHANGE"

    review = item.get("review") or {}
    review_status = review.get("overall_status")
    overrides = review.get("override_values") or {}
    changes: list[dict[str, Any]] = []
    for field in AUDIT_FIELDS:
        before = original.get(field)
        proposed = proposals.get(field)
        if review_status == "OVERRIDDEN" and overrides.get(field) is not None:
            final = overrides[field]
            action = "OVERRIDDEN"
        elif policy == "AUTO_APPLY" and proposed is not None:
            final = proposed
            action = "AUTO_APPLY"
        elif review_status == "APPROVED" and proposed is not None:
            final = proposed
            action = "APPROVED"
        else:
            final = before
            action = policy if proposed is not None else "NO_CHANGE"
        provenance = (item.get("field_provenance") or {}).get(field) or {}
        changes.append({
            "field": field,
            "original": before,
            "proposed": proposed,
            "final": final,
            "action": action,
            "method": provenance.get("method", item.get("method", "NONE")),
            "rule_id": provenance.get("rule_id") or (item.get("rule") or {}).get("rule_id"),
            "confidence": provenance.get("confidence") or item.get("confidence"),
        })

    item["findings"] = findings
    item["changes"] = changes
    item["application_policy"] = policy
    item["result_ledger_version"] = RESULT_LEDGER_VERSION
    return item


def changes_after_review(
    item: dict[str, Any],
    status: str,
    overrides: dict[str, object] | None = None,
) -> list[dict[str, Any]]:
    """Return a refreshed immutable change ledger for a review decision."""
    override_values = overrides or {}
    refreshed: list[dict[str, Any]] = []
    for source in item.get("changes") or []:
        change = deepcopy(source)
        field = str(change.get("field"))
        if status == "OVERRIDDEN" and field in override_values:
            change["final"] = override_values[field]
            change["action"] = "OVERRIDDEN"
            change["method"] = "HUMAN_OVERRIDE"
        elif status == "APPROVED" and change.get("proposed") is not None:
            change["final"] = change["proposed"]
            change["action"] = "APPROVED"
        elif status == "REJECTED":
            change["final"] = change.get("original")
            change["action"] = "REJECTED" if change.get("proposed") is not None else "NO_CHANGE"
        refreshed.append(change)
    return refreshed
