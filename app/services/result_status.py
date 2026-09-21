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

_PURGED = {"group": "SKIPPED_PURGED"}
_UNRESOLVED = {
    "group": "C",
    "field_proposals.standard_size": None,
    "field_proposals.standard_uom": None,
}
_NEEDS_REVIEW = {"$or": [
    {"application_policy": "REVIEW_REQUIRED"},
    {"findings.code": {"$in": sorted(REVIEW_CODES)}},
]}


def effective_status(item: dict[str, Any]) -> str:
    if item.get("group") == "SKIPPED_PURGED":
        return "SKIPPED"
    proposals = item.get("field_proposals") or {}
    if (
        item.get("group") == "C"
        and proposals.get("standard_size") is None
        and proposals.get("standard_uom") is None
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
