"""Open questions: every row that is not simply "already correct", grouped by the kind
of problem, with all its data, so a business reviewer can answer per category.

Read-only. Nothing here changes a row, the export or the engine.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import yaml

from app.services.export_service import row_comment
from app.services.job_comparison_service import FIELDS, _values, describe, outcome
from app.services.result_status import GROUP_LABELS, STATUS_LABELS, effective_status
from app.services.rule_engine import RuleEngine

OPEN_QUESTIONS_PATH = Path(__file__).resolve().parents[1] / "evaluations" / "open_questions.v1.yaml"
DESCRIPTION_FIELDS = (
    ("item_brand_eng", "Brand (English)"),
    ("item_brand_local_lang", "Brand (local language)"),
    ("item_desc_eng", "Item description (English)"),
    ("item_desc_local_lang", "Item description (local language)"),
    ("web_description_eng", "Web description (English)"),
    ("web_description_chi", "Web description (local language)"),
)


@lru_cache(maxsize=1)
def load_catalogue() -> dict[str, Any]:
    document = yaml.safe_load(OPEN_QUESTIONS_PATH.read_text(encoding="utf-8")) or {}
    return {
        "version": str(document.get("version") or ""),
        "groups": list(document.get("groups") or []),
        "categories": list(document.get("categories") or []),
    }


def categorize(item: dict[str, Any], status: str) -> str | None:
    """The first catalogue category whose conditions the row meets; None for
    rows that are already correct or skipped."""
    if status in {"NO_CHANGE", "SKIPPED"}:
        return None
    codes = {str(f.get("code")) for f in item.get("findings") or []}
    uom = str((item.get("original") or {}).get("standard_uom") or "").strip().upper()
    reason = str(item.get("reason_code") or "")
    for category in load_catalogue()["categories"]:
        wanted_status = category.get("status")
        if wanted_status and status != wanted_status:
            continue
        if category.get("uom") and uom != category["uom"]:
            continue
        if category.get("reason"):
            if reason == category["reason"] and status == "AUTO_APPLY":
                return category["id"]
            continue
        wanted_codes = category.get("codes")
        if wanted_codes:
            if codes & set(wanted_codes):
                return category["id"]
            continue
        # A catch-all category: only its status condition applies.
        if wanted_status:
            return category["id"]
    return None


def _legacy_reading(engine: RuleEngine, original: dict[str, Any]) -> dict[str, Any] | None:
    size, uom = original.get("legacy_size"), original.get("legacy_uom")
    if size in (None, "") or not uom:
        return None
    proposal = engine.propose(size, uom)
    if proposal is None:
        return {"text": f"{size} {uom}".strip(), "converted": None}
    identity = proposal.source_uom == proposal.target_uom and proposal.factor == Decimal("1")
    converted = proposal.raw_target if identity else proposal.standard_size
    return {
        "text": f"{size} {uom}".strip(),
        "converted": f"{format(converted.normalize(), 'f')} {proposal.standard_uom}",
        "converted_size": format(converted.normalize(), "f"),
        "converted_uom": proposal.standard_uom,
    }


def build_row(item: dict[str, Any], engine: RuleEngine) -> dict[str, Any] | None:
    status = effective_status(item)
    category = categorize(item, status)
    if category is None:
        return None
    original = item.get("original") or {}
    context = item.get("context") or {}
    result = outcome(item)
    uploaded = _values(*(original.get(field) for field in FIELDS))
    legacy = _legacy_reading(engine, original)
    options: list[dict[str, Any]] = [{
        "label": "Keep Excel as uploaded", "values": uploaded, "text": describe(uploaded),
        "source": "EXCEL",
    }]
    if result["status"] == "AUTO_APPLY":
        options.append({
            "label": "Applied automatically", "values": result["values"],
            "text": describe(result["values"]), "source": "APPLIED",
        })
    if result["suggestion"] is not None:
        options.append({
            "label": "Use the suggestion", "values": result["suggestion"],
            "text": describe(result["suggestion"]), "source": "SUGGESTION",
        })
    # The converted legacy is offered only when nothing else is suggested; next to a
    # linked suggestion it would show the very size-only reading the engine rejected.
    if legacy and legacy.get("converted_size") and result["suggestion"] is None and result["status"] != "AUTO_APPLY":
        reading = _values(legacy["converted_size"], legacy["converted_uom"], original.get("standard_pack_size"))
        if not any(o["values"] == reading for o in options):
            options.append({
                "label": "Legacy value as converted", "values": reading,
                "text": describe(reading), "source": "LEGACY",
            })
    group = str(item.get("group") or "")
    return {
        "row_number": item.get("row_number"),
        "item_no": str(item.get("item_no") or ""),
        "group": group,
        "group_label": GROUP_LABELS.get(group, group),
        "category": category,
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "descriptions": [
            {"field": field, "label": label, "value": context.get(field)}
            for field, label in DESCRIPTION_FIELDS
        ],
        "product": context.get("item_desc_eng") or context.get("web_description_eng") or "",
        "product_local": context.get("item_desc_local_lang") or context.get("web_description_chi") or "",
        "legacy": legacy,
        "excel": uploaded,
        "result": result["values"],
        "suggestion": result["suggestion"],
        "comment": result["comment"],
        "options": options,
        "findings": [
            {
                "code": f.get("code"), "title": f.get("title"), "severity": f.get("severity"),
                "message": f.get("human_reason") or f.get("message"),
                "evidence": list(f.get("evidence") or []),
            }
            for f in item.get("findings") or []
        ],
    }


class OpenQuestionsService:
    def __init__(self, engine: RuleEngine):
        self.engine = engine

    def build(self, items: Iterable[dict[str, Any]], answers: dict[str, dict[str, Any]]) -> dict[str, Any]:
        catalogue = load_catalogue()
        rows: list[dict[str, Any]] = []
        for item in items:
            row = build_row(item, self.engine)
            if row is not None:
                rows.append(row)
        counts = Counter(row["category"] for row in rows)
        status_counts: dict[str, Counter[str]] = {}
        for row in rows:
            status_counts.setdefault(row["category"], Counter())[row["status"]] += 1
        categories = []
        for category in catalogue["categories"]:
            total = counts.get(category["id"], 0)
            if total == 0 and category["id"].startswith("other_"):
                continue
            categories.append({
                "id": category["id"], "group": category["group"], "kind": category["kind"],
                "name": category["name"], "rule": category.get("rule", ""),
                "question": category.get("question", ""), "options": list(category.get("options") or []),
                "rows": total,
                "statuses": dict(status_counts.get(category["id"], {})),
                "answer": answers.get(category["id"]),
            })
        groups = [
            {**group, "rows": sum(c["rows"] for c in categories if c["group"] == group["id"]),
             "categories": [c for c in categories if c["group"] == group["id"]]}
            for group in catalogue["groups"]
        ]
        return {
            "version": catalogue["version"],
            "rows_total": len(rows),
            "answered": sum(1 for c in categories if c["answer"]),
            "groups": groups,
            "rows": rows,
        }


def filter_rows(rows: list[dict[str, Any]], *, category: str, search: str | None, group: str | None,
                status: str | None) -> list[dict[str, Any]]:
    needle = (search or "").strip().lower()
    out = []
    for row in rows:
        if row["category"] != category:
            continue
        if group and row["group"] != group:
            continue
        if status and row["status"] != status:
            continue
        if needle and needle not in f"{row['item_no']} {row['product']} {row['product_local']}".lower():
            continue
        out.append(row)
    return out
