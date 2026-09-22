"""The one status a stakeholder sees for a row, for the ledger, its counts and filters.

It is derived from stored fields rather than read from ``application_policy`` alone, so
jobs processed by an earlier ledger version show the same statuses as new ones. The
Python function and the MongoDB query are two views of the same rule and are tested
together.
"""
from __future__ import annotations

from typing import Any

from app.services.result_ledger_service import REVIEW_CODES

# Display order.
STATUSES = ("NO_CHANGE", "AUTO_APPLY", "OBSERVATION_ONLY", "REVIEW_REQUIRED", "UNRESOLVED", "SKIPPED")

# One wording for a status, shared by the screen and the exported workbook so the two
# can never drift apart.
STATUS_LABELS = {
    "NO_CHANGE": "Already correct",
    "AUTO_APPLY": "Corrected automatically",
    "OBSERVATION_ONLY": "Correct — with a note",
    "REVIEW_REQUIRED": "Needs your review",
    "UNRESOLVED": "Could not determine",
    "SKIPPED": "Skipped — purged",
}

# What kind of row this was, not what happened to it. "A (disputed)" is an A-shaped row
# whose complete values are contradicted by the product's own description, so it cannot
# be trusted as A; every other group keeps the letter the team already uses.
GROUP_LABELS = {
    "A": "A",
    "B": "B",
    "C": "C",
    "VALIDATION_REVIEW": "A (disputed)",
    "SKIPPED_PURGED": "Purged",
    "DATA_SHAPE_ERROR": "Invalid data",
    "OUT_OF_SCOPE": "Out of scope",
}


def status_label(item: dict[str, Any]) -> str:
    status = effective_status(item)
    return STATUS_LABELS.get(status, status)


def group_label(item: dict[str, Any]) -> str:
    group = str(item.get("group") or "")
    return GROUP_LABELS.get(group, group)

_PURGED = {"group": "SKIPPED_PURGED"}
# Nothing could be worked out: either the AI found no size in the text (C), or the
# legacy unit has no agreed conversion, so a rule could not fill the blank (B).
NO_CONVERSION_REASONS = ("NO_RULE", "MALFORMED_VALUE")
_NO_SIZE_PROPOSED = {
    "field_proposals.standard_size": None,
    "field_proposals.standard_uom": None,
}
_UNRESOLVED = {"$or": [
    {"group": "C", **_NO_SIZE_PROPOSED},
    {"group": "B", "reason_code": {"$in": list(NO_CONVERSION_REASONS)}, **_NO_SIZE_PROPOSED},
]}
_NEEDS_REVIEW = {"$or": [
    {"application_policy": "REVIEW_REQUIRED"},
    {"findings.code": {"$in": sorted(REVIEW_CODES)}},
]}


def effective_status(item: dict[str, Any]) -> str:
    if item.get("group") == "SKIPPED_PURGED":
        return "SKIPPED"
    proposals = item.get("field_proposals") or {}
    no_size = proposals.get("standard_size") is None and proposals.get("standard_uom") is None
    if no_size and (
        item.get("group") == "C"
        or (item.get("group") == "B" and item.get("reason_code") in NO_CONVERSION_REASONS)
    ):
        return "UNRESOLVED"
    codes = {finding.get("code") for finding in item.get("findings") or []}
    if item.get("application_policy") == "REVIEW_REQUIRED" or codes & REVIEW_CODES:
        return "REVIEW_REQUIRED"
    policy = item.get("application_policy") or "NO_CHANGE"
    # A stored UNRESOLVED that now has a value is simply an applied result.
    return policy if policy in {"NO_CHANGE", "AUTO_APPLY", "OBSERVATION_ONLY"} else "NO_CHANGE"


def status_query(status: str) -> dict[str, Any]:
    """MongoDB filter selecting exactly the rows ``effective_status`` maps to ``status``."""
    if status == "SKIPPED":
        return dict(_PURGED)
    if status == "UNRESOLVED":
        return dict(_UNRESOLVED)
    not_earlier = [{"$nor": [_PURGED, _UNRESOLVED]}]
    if status == "REVIEW_REQUIRED":
        return {"$and": [*not_earlier, _NEEDS_REVIEW]}
    if status == "NO_CHANGE":
        own = {"application_policy": {"$nin": ["AUTO_APPLY", "OBSERVATION_ONLY", "REVIEW_REQUIRED"]}}
    elif status in {"AUTO_APPLY", "OBSERVATION_ONLY"}:
        own = {"application_policy": status}
    else:
        raise ValueError(f"unknown status: {status}")
    return {"$and": [*not_earlier, {"$nor": [_NEEDS_REVIEW]}, own]}
