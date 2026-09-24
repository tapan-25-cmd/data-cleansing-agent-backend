"""The route: which method the tool used on a row, chosen before any result exists.

A row's *group* (A, B, C, Purged) is the outcome and is derived from its final status
(see result_status). The route is the method: check what is filled, convert the old
size, read the description, complete a half-filled row, or skip. Rows stored before
24 September 2026 kept the route under the key "group"; route_of reads both.
"""
from __future__ import annotations

from typing import Any

ROUTE_LABELS = {
    "A": "Checked existing values",
    "VALIDATION_REVIEW": "Checked existing values",
    "B": "Converted from the old size",
    "C": "Read from the description",
    "INCOMPLETE": "Half-filled row",
    "DATA_SHAPE_ERROR": "Values could not be used",
    "SKIPPED_PURGED": "Skipped",
    "OUT_OF_SCOPE": "Out of scope",
}


def route_of(item: dict[str, Any]) -> str:
    route = item.get("route")
    if route:
        return str(route)
    # Legacy documents and fixtures: the route was stored as "group".
    return str(item.get("group") or "")


def route_label(item: dict[str, Any]) -> str:
    route = route_of(item)
    return ROUTE_LABELS.get(route, route)
