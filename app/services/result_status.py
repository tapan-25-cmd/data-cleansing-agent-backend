"""The one status and the one group a stakeholder sees for a row.

Status: what happened to the row (Already correct, Corrected automatically, ...).
Group: the outcome, decided last, purely from the status:
    A       the tool changed nothing          (Already correct, Correct with a note)
    B       the tool changed K, L or M itself (Corrected automatically)
    C       the tool raised it to a person    (Needs your review, Could not determine, Invalid)
    PURGED  skipped before anything ran
A person's later decision does not move a row between groups.

Both are derived from stored fields rather than stored themselves, so jobs processed by
an earlier version show the same statuses and groups as new ones. Each Python function
has a MongoDB query twin and the two are tested together.
"""
from __future__ import annotations

from typing import Any

from app.services.result_ledger_service import REVIEW_CODES, differs  # noqa: F401  (re-exported)
from app.services.routes import ROUTE_LABELS, route_label, route_of  # noqa: F401  (re-exported)

# Display order.
STATUSES = ("NO_CHANGE", "AUTO_APPLY", "OBSERVATION_ONLY", "REVIEW_REQUIRED", "UNRESOLVED", "INVALID", "SKIPPED")

# One wording for a status, shared by the screen and the exported workbook so the two
# can never drift apart.
STATUS_LABELS = {
    "NO_CHANGE": "Already correct",
    "AUTO_APPLY": "Corrected automatically",
    "OBSERVATION_ONLY": "Correct — with a note",
    "REVIEW_REQUIRED": "Needs your review",
    "UNRESOLVED": "Could not determine",
    "INVALID": "Invalid values",
    "SKIPPED": "Skipped — purged",
}

GROUPS = ("A", "B", "C", "PURGED")
GROUP_OF_STATUS = {
    "NO_CHANGE": "A",
    "OBSERVATION_ONLY": "A",
    "AUTO_APPLY": "B",
    "REVIEW_REQUIRED": "C",
    "UNRESOLVED": "C",
    "INVALID": "C",
    "SKIPPED": "PURGED",
}
STATUSES_OF_GROUP = {
    group: tuple(status for status in STATUSES if GROUP_OF_STATUS[status] == group) for group in GROUPS
}
GROUP_LABELS = {"A": "A", "B": "B", "C": "C", "PURGED": "Purged"}
GROUP_NAMES = {
    "A": "No change",
    "B": "Changed by the tool",
    "C": "Raised for a person",
    "PURGED": "Purged",
}


def status_label(item: dict[str, Any]) -> str:
    status = effective_status(item)
    return STATUS_LABELS.get(status, status)


def outcome_group(item: dict[str, Any]) -> str:
    return GROUP_OF_STATUS[effective_status(item)]


def group_label(item: dict[str, Any]) -> str:
    return GROUP_LABELS[outcome_group(item)]


_PURGED = {"route": "SKIPPED_PURGED"}
# A value in the row cannot be used at all (text where a number should be, an unknown
# unit): the row can be neither checked nor filled and is reported as it is.
_INVALID = {"route": "DATA_SHAPE_ERROR"}
# Nothing could be worked out: either the AI found no size in the text (C), or the
# legacy unit has no agreed conversion, so a rule could not fill the blank (B).
NO_CONVERSION_REASONS = ("NO_RULE", "MALFORMED_VALUE")
_NO_SIZE_PROPOSED = {
    "field_proposals.standard_size": None,
    "field_proposals.standard_uom": None,
}
_UNRESOLVED = {"$or": [
    {"route": "C", **_NO_SIZE_PROPOSED},
    {"route": "B", "reason_code": {"$in": list(NO_CONVERSION_REASONS)}, **_NO_SIZE_PROPOSED},
]}
_NEEDS_REVIEW = {"$or": [
    {"application_policy": "REVIEW_REQUIRED"},
    {"findings.code": {"$in": sorted(REVIEW_CODES)}},
]}


def effective_status(item: dict[str, Any]) -> str:
    route = route_of(item)
    if route == "SKIPPED_PURGED":
        return "SKIPPED"
    if route == "DATA_SHAPE_ERROR":
        return "INVALID"
    proposals = item.get("field_proposals") or {}
    no_size = proposals.get("standard_size") is None and proposals.get("standard_uom") is None
    if no_size and (
        route == "C"
        or (route == "B" and item.get("reason_code") in NO_CONVERSION_REASONS)
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
    if status == "INVALID":
        return dict(_INVALID)
    if status == "UNRESOLVED":
        return {"$and": [{"$nor": [_PURGED, _INVALID]}, _UNRESOLVED]}
    not_earlier = [{"$nor": [_PURGED, _INVALID, _UNRESOLVED]}]
    if status == "REVIEW_REQUIRED":
        return {"$and": [*not_earlier, _NEEDS_REVIEW]}
    if status == "NO_CHANGE":
        own = {"application_policy": {"$nin": ["AUTO_APPLY", "OBSERVATION_ONLY", "REVIEW_REQUIRED"]}}
    elif status in {"AUTO_APPLY", "OBSERVATION_ONLY"}:
        own = {"application_policy": status}
    else:
        raise ValueError(f"unknown status: {status}")
    return {"$and": [*not_earlier, {"$nor": [_NEEDS_REVIEW]}, own]}


def group_query(group: str) -> dict[str, Any]:
    """MongoDB filter selecting exactly the rows ``outcome_group`` maps to ``group``."""
    if group not in STATUSES_OF_GROUP:
        raise ValueError(f"unknown group: {group}")
    return {"$or": [status_query(status) for status in STATUSES_OF_GROUP[group]]}


# How a row's values came about, for the export's "How" column and the screens.
_RULE_SOURCES = {
    "STANDARD_FIELDS_CANONICALIZATION": "Unit spelling fixed",
    "SINGLE_ITEM_DEFAULT": "Pack rule: single item",
    "GAP_FROM_LEGACY": "Old size, unit table",
    "GAP_FROM_TEXT": "Read from the description",
    "GAP_PACK_FROM_TEXT": "Pack count from the description",
}


def how_label(item: dict[str, Any]) -> str:
    """One short phrase: for a changed row, where each change came from; otherwise the
    method the tool used on the row."""
    status = effective_status(item)
    if status == "SKIPPED":
        return "Skipped, purged"
    if status != "AUTO_APPLY":
        return route_label(item)
    sources: list[str] = []
    provenance = item.get("field_provenance") or {}
    proposals = item.get("field_proposals") or {}
    original = item.get("original") or {}
    for field in ("standard_size", "standard_uom", "standard_pack_size"):
        if proposals.get(field) is None or not differs(proposals.get(field), original.get(field)):
            continue
        entry = provenance.get(field) or {}
        method = str(entry.get("method") or "")
        rule_id = str(entry.get("rule_id") or "")
        if method == "AI_INFERENCE":
            source = "Pack count read from the description" if field == "standard_pack_size" else "Read from the description"
        elif rule_id in _RULE_SOURCES:
            source = _RULE_SOURCES[rule_id]
        elif field == "standard_pack_size" and method == "RULE":
            source = "Pack value tidied" if rule_id == "PACK_VALUE_NORMALIZATION" else "Pack count from the description"
        elif method == "RULE":
            # Converted from the old size when Excel had no size; otherwise Excel's own
            # value was converted to a standard unit (1 KG → 1000 GM).
            source = "Old size, unit table" if original.get("standard_size") in (None, "") else "Unit table"
        else:
            source = route_label(item)
        if source not in sources:
            sources.append(source)
    return " + ".join(sources) if sources else route_label(item)


def describe(item: dict[str, Any]) -> dict[str, Any]:
    """Add the derived status, outcome group, method and "how" to a row sent to a screen."""
    item["status"] = effective_status(item)
    item["status_label"] = STATUS_LABELS[item["status"]]
    item["group"] = GROUP_OF_STATUS[item["status"]]
    item["route"] = route_of(item)
    item["route_label"] = route_label(item)
    item["how"] = how_label(item)
    return item
