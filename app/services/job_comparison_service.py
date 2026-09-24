"""Past run versus new run: what the previous processing said for every row, what the
current processing says, and whether reviewer-confirmed answers are now reached.

Two complete jobs of the same workbook are compared row by row (by Excel row number).
Nothing is written; both jobs stay exactly as processed.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import yaml

from app.services.export_service import row_comment
from app.services.result_status import GROUP_LABELS, STATUS_LABELS, effective_status

JOB_COMPARISON_VERSION = "job-comparison-v2"
REVIEWER_CASES_PATH = Path(__file__).resolve().parents[1] / "evaluations" / "reviewer_confirmed_cases.v1.yaml"

FIELDS = ("standard_size", "standard_uom", "standard_pack_size")

# What changed between the two runs, in the order a reader cares about.
CHANGE_LABELS = {
    "SAME": "Same result",
    "REVIEW_CLEARED": "No longer needs review",
    "NOW_AUTOMATIC": "Now corrected automatically",
    "VALUES_CHANGED": "Different final values",
    "SUGGESTION_CHANGED": "Different suggestion",
    "NEW_REVIEW": "Now needs review",
    "NOW_UNRESOLVED": "Now could not determine",
    "NOTE_CHANGED": "Note added or removed",
}

REVIEWER_VERDICT_LABELS = {
    "ACHIEVED": "Matches the reviewer",
    "KEPT_UNDER_REVIEW": "Reviewer's value is kept, but the row still waits for a review",
    "SUGGESTED": "Reviewer's value is suggested, waiting for approval",
    "CONTRARY_PENDING": "Right value kept, but a wrong suggestion is still waiting for review",
    "NOT_ACHIEVED": "Does not match the reviewer",
    "NOT_IN_RUN": "Product not in this run",
}


def _decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _plain(value: object) -> str | None:
    number = _decimal(value)
    if number is None:
        return None if value in (None, "") else str(value)
    return format(number.normalize(), "f")


def _uom(value: object) -> str | None:
    text = str(value or "").strip().upper()
    return text or None


def _values(size: object, uom: object, pack: object) -> dict[str, str | None]:
    k, m = _decimal(size), _decimal(pack)
    return {
        "standard_size": _plain(size),
        "standard_uom": _uom(uom),
        "standard_pack_size": _plain(pack),
        "total": _plain(k * m) if k is not None and m is not None else None,
    }


def _apply(base: dict[str, Any], overlay: dict[str, Any] | None) -> dict[str, str | None]:
    merged = {field: base.get(field) for field in FIELDS}
    for field in FIELDS:
        value = (overlay or {}).get(field)
        if value is not None and value != "":
            merged[field] = value
    return _values(merged["standard_size"], merged["standard_uom"], merged["standard_pack_size"])


def _same(a: dict[str, str | None], b: dict[str, str | None]) -> bool:
    return all(a.get(field) == b.get(field) for field in FIELDS)


def describe(values: dict[str, str | None]) -> str:
    if not any(values.get(field) for field in FIELDS):
        return "blank"
    unit = " ".join(part for part in (values.get("standard_size"), values.get("standard_uom")) if part) or "no size"
    pack = values.get("standard_pack_size")
    return f"{unit} × {pack}" if pack else unit


def outcome(item: dict[str, Any]) -> dict[str, Any]:
    """One run's answer for a row: status, the values the workbook would carry, and
    any suggestion still waiting for a person."""
    status = effective_status(item)
    original = item.get("original") or {}
    review = item.get("review") or {}
    proposals = item.get("field_proposals") or {}
    uploaded = _values(*(original.get(field) for field in FIELDS))
    review_status = str(review.get("overall_status") or "")
    if review_status == "OVERRIDDEN":
        final = _apply(original, review.get("override_values"))
    elif status == "AUTO_APPLY" or review_status == "APPROVED":
        final = _apply(original, proposals)
    else:
        final = uploaded
    suggestion = None
    if status == "REVIEW_REQUIRED" and any(proposals.get(field) is not None for field in FIELDS):
        suggestion = _apply(original, proposals)
    return {
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "values": final,
        "suggestion": suggestion,
        "comment": row_comment(item),
    }


def classify(past: dict[str, Any], new: dict[str, Any]) -> str:
    same_values = _same(past["values"], new["values"])
    same_suggestion = (past["suggestion"] is None and new["suggestion"] is None) or (
        past["suggestion"] is not None and new["suggestion"] is not None
        and _same(past["suggestion"], new["suggestion"])
    )
    if past["status"] == new["status"] and same_values and same_suggestion:
        return "SAME"
    if new["status"] == "AUTO_APPLY" and past["status"] != "AUTO_APPLY":
        return "NOW_AUTOMATIC"
    if not same_values:
        return "VALUES_CHANGED"
    if past["status"] == "REVIEW_REQUIRED" and new["status"] in {"NO_CHANGE", "OBSERVATION_ONLY"}:
        return "REVIEW_CLEARED"
    if new["status"] == "REVIEW_REQUIRED" and past["status"] != "REVIEW_REQUIRED":
        return "NEW_REVIEW"
    if new["status"] == "UNRESOLVED" and past["status"] != "UNRESOLVED":
        return "NOW_UNRESOLVED"
    if not same_suggestion:
        return "SUGGESTION_CHANGED"
    return "NOTE_CHANGED"


def explain(change: str, past: dict[str, Any], new: dict[str, Any]) -> str:
    if change == "SAME":
        return "Both runs reached the same result."
    if change == "REVIEW_CLEARED":
        return (
            f"The past run asked a person to check this row. The new run keeps "
            f"{describe(new['values'])} and explains why no review is needed."
        )
    if change == "NOW_AUTOMATIC":
        return f"The new run fills in {describe(new['values'])} automatically; the past run did not."
    if change == "VALUES_CHANGED":
        return f"The workbook would carry {describe(past['values'])} after the past run and {describe(new['values'])} after the new run."
    if change == "SUGGESTION_CHANGED":
        return (
            f"Both runs ask for a review, but suggest different values: "
            f"{describe(past['suggestion'] or {})} before, {describe(new['suggestion'] or {})} now."
        )
    if change == "NEW_REVIEW":
        return "The new run found something a person should check that the past run did not."
    if change == "NOW_UNRESOLVED":
        return "The new run could not determine a value where the past run did."
    return "The values are the same; only the note attached to the row changed."


@lru_cache(maxsize=1)
def load_reviewer_cases() -> tuple[str, str, tuple[dict[str, Any], ...]]:
    document = yaml.safe_load(REVIEWER_CASES_PATH.read_text(encoding="utf-8")) or {}
    cases = tuple(
        {**case, "expected": _values(*(case["expected"].get(field) for field in FIELDS))}
        for case in document.get("cases") or []
    )
    return str(document.get("version") or ""), str(document.get("reviewer") or ""), cases


def reviewer_verdict(expected: dict[str, str | None], result: dict[str, Any] | None) -> str:
    if result is None:
        return "NOT_IN_RUN"
    suggestion = result.get("suggestion")
    if _same(result["values"], expected):
        if suggestion is not None and not _same(suggestion, expected):
            return "CONTRARY_PENDING"
        if result.get("status") == "REVIEW_REQUIRED":
            return "KEPT_UNDER_REVIEW"
        return "ACHIEVED"
    if suggestion is not None and _same(suggestion, expected):
        return "SUGGESTED"
    return "NOT_ACHIEVED"


class JobComparisonService:
    """Compare every row of a past job with the same row of a new job."""

    def build(
        self,
        past_items: Iterable[dict[str, Any]],
        new_items: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        past_by_row = {item["row_number"]: item for item in past_items}
        rows: list[dict[str, Any]] = []
        by_change: Counter[str] = Counter()
        past_status: Counter[str] = Counter()
        new_status: Counter[str] = Counter()
        groups: Counter[str] = Counter()
        by_item: dict[str, dict[str, Any]] = {}
        missing_in_past = 0
        for item in new_items:
            past_item = past_by_row.get(item["row_number"])
            if past_item is None:
                missing_in_past += 1
                continue
            new = outcome(item)
            if new["status"] == "SKIPPED" and effective_status(past_item) == "SKIPPED":
                continue
            past = outcome(past_item)
            change = classify(past, new)
            context = item.get("context") or {}
            group = str(item.get("group") or "")
            row = {
                "row_number": item["row_number"],
                "item_no": str(item.get("item_no") or ""),
                "group": group,
                "group_label": GROUP_LABELS.get(group, group),
                "product": context.get("item_desc_eng") or context.get("web_description_eng") or "",
                "product_local": context.get("item_desc_local_lang") or context.get("web_description_chi") or "",
                "uploaded": _values(*((item.get("original") or {}).get(field) for field in FIELDS)),
                "legacy": " ".join(
                    part for part in (
                        _plain((item.get("original") or {}).get("legacy_size")),
                        str((item.get("original") or {}).get("legacy_uom") or "").strip(),
                    ) if part
                ) or None,
                "past": past,
                "new": new,
                "change": change,
                "change_label": CHANGE_LABELS[change],
                "explanation": explain(change, past, new),
                "reviewer": None,
            }
            rows.append(row)
            by_item.setdefault(row["item_no"], row)
            by_change[change] += 1
            past_status[past["status"]] += 1
            new_status[new["status"]] += 1
            groups[group] += 1

        cases_version, reviewer, cases = load_reviewer_cases()
        reviewer_rows = []
        past_verdicts: Counter[str] = Counter()
        new_verdicts: Counter[str] = Counter()
        for case in cases:
            row = by_item.get(case["item_no"]) or by_item.get(case["item_no"].lstrip("0"))
            past_verdict = reviewer_verdict(case["expected"], row["past"] if row else None)
            new_verdict = reviewer_verdict(case["expected"], row["new"] if row else None)
            past_verdicts[past_verdict] += 1
            new_verdicts[new_verdict] += 1
            entry = {
                "item_no": case["item_no"],
                "product": row["product"] if row else "",
                "row_number": row["row_number"] if row else None,
                "answer": case.get("answer") or "",
                "basis": case.get("basis") or "",
                "expected": case["expected"],
                "past": {
                    "verdict": past_verdict,
                    "verdict_label": REVIEWER_VERDICT_LABELS[past_verdict],
                    "status": row["past"]["status"] if row else None,
                    "values": row["past"]["values"] if row else None,
                    "suggestion": row["past"]["suggestion"] if row else None,
                },
                "new": {
                    "verdict": new_verdict,
                    "verdict_label": REVIEWER_VERDICT_LABELS[new_verdict],
                    "status": row["new"]["status"] if row else None,
                    "values": row["new"]["values"] if row else None,
                    "suggestion": row["new"]["suggestion"] if row else None,
                },
            }
            reviewer_rows.append(entry)
            if row:
                row["reviewer"] = {
                    "answer": entry["answer"], "basis": entry["basis"], "expected": case["expected"],
                    "past_verdict": past_verdict, "new_verdict": new_verdict,
                    "past_verdict_label": REVIEWER_VERDICT_LABELS[past_verdict],
                    "new_verdict_label": REVIEWER_VERDICT_LABELS[new_verdict],
                }

        return {
            "version": JOB_COMPARISON_VERSION,
            "mode": "JOB_VS_JOB",
            "summary": {
                "rows_compared": len(rows),
                "changed": sum(count for change, count in by_change.items() if change != "SAME"),
                "missing_in_past": missing_in_past,
                "by_change": {change: by_change.get(change, 0) for change in CHANGE_LABELS},
                "past_status": dict(past_status),
                "new_status": dict(new_status),
            },
            "change_labels": dict(CHANGE_LABELS),
            "status_labels": dict(STATUS_LABELS),
            "reviewer": {
                "version": cases_version,
                "reviewer": reviewer,
                "total": len(cases),
                "past": {verdict: past_verdicts.get(verdict, 0) for verdict in REVIEWER_VERDICT_LABELS},
                "new": {verdict: new_verdicts.get(verdict, 0) for verdict in REVIEWER_VERDICT_LABELS},
                "verdict_labels": dict(REVIEWER_VERDICT_LABELS),
                "cases": reviewer_rows,
            },
            "facets": {
                "change": dict(by_change),
                "past_status": dict(past_status),
                "new_status": dict(new_status),
                "group": dict(groups),
            },
            "rows": rows,
        }


def filter_rows(rows: list[dict[str, Any]], **filters: str | None) -> list[dict[str, Any]]:
    change = filters.get("change")
    past_status = filters.get("past_status")
    new_status = filters.get("new_status")
    group = filters.get("group")
    search = (filters.get("search") or "").strip().lower()
    changed_only = filters.get("changed_only")
    out = []
    for row in rows:
        if changed_only and row["change"] == "SAME":
            continue
        if change and row["change"] != change:
            continue
        if past_status and row["past"]["status"] != past_status:
            continue
        if new_status and row["new"]["status"] != new_status:
            continue
        if group and row["group"] != group:
            continue
        if search and search not in f"{row['item_no']} {row['product']} {row['product_local']}".lower():
            continue
        out.append(row)
    return out
