"""Blind comparison of product-photo readings with B/C K/L/M outcomes.

The pack reader has already seen only gallery images and a number-free identity hint.
This module deliberately receives its persisted result first and reconciles it with the
answer key afterwards. Nothing here changes a cleansing result.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from app.agents.pack_reader import PACK_READER_VERSION

VERSION = "bc-image-blind-test-v1"
FACTORS: dict[str, tuple[str, Decimal]] = {
    "GM": ("GM", Decimal("1")), "KG": ("GM", Decimal("1000")),
    "ML": ("ML", Decimal("1")), "L": ("ML", Decimal("1000")),
    "EA": ("EA", Decimal("1")), "OZ": ("GM", Decimal("28.35")),
    "LB": ("GM", Decimal("453.6")),
}

AUDITED = {
    **{x: ("RAW_EXCEL_LIKELY_WRONG", "RAW EXCEL DATA", "Review and correct the source value") for x in
       ("357731", "032706", "607218", "607309")},
    **{x: ("OUR_PROPOSAL_LIKELY_WRONG", "CLEANSING PROPOSAL", "Reject or recalculate the proposal") for x in
       ("104687", "287508", "777185")},
    **{x: ("PACKAGING_DECISION_REQUIRED", "BUSINESS POLICY", "Confirm which packaging level M represents") for x in
       ("236638", "238790", "240572")},
    **{x: ("DIFFERENT_MEASUREMENT_REPRESENTATION", "MEASUREMENT REPRESENTATION", "Do not count as wrong without a preferred-unit policy") for x in
       ("263483", "264267", "387183", "647263", "647602", "647735", "647628", "647701",
        "157990", "024588", "269290", "605808", "158022", "270751", "533489", "338509",
        "338665", "335950", "455220", "455238", "687699", "691279", "032755", "455287")},
}


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "").strip()) if value not in (None, "") else None
    except (InvalidOperation, ValueError, AttributeError):
        return None


def _plain(value: Decimal | None) -> str | None:
    return format(value.normalize(), "f") if value is not None else None


def normalize(values: dict[str, Any] | None) -> dict[str, Any] | None:
    if not values:
        return None
    size = _decimal(values.get("standard_size", values.get("size")))
    count = _decimal(values.get("standard_pack_size", values.get("total_count", values.get("count")))) or Decimal("1")
    uom = str(values.get("standard_uom", values.get("uom")) or "").strip().upper()
    if size is None or size <= 0 or count <= 0 or uom not in FACTORS:
        return None
    base, factor = FACTORS[uom]
    unit = size * factor
    return {"size": _plain(size), "uom": uom, "count": _plain(count),
            "base_uom": base, "base_unit": unit, "base_total": unit * count}


def compare(reading: dict[str, Any], expected: dict[str, Any] | None) -> dict[str, Any]:
    if reading.get("error"):
        return {"verdict": "TECHNICAL_ERROR", "comparable": False, "scored": False,
                "reason": reading["error"]}
    if reading.get("other_product_only"):
        return {"verdict": "OTHER_PRODUCT_IMAGE", "comparable": False, "scored": False,
                "reason": "The available photos show another product."}
    if not reading.get("legible"):
        return {"verdict": "NO_READABLE_QUANTITY", "comparable": False, "scored": False,
                "reason": reading.get("note") or "No readable net quantity was visible."}
    image = normalize(reading)
    ours = normalize(expected)
    if not image or not ours:
        return {"verdict": "NOT_COMPARABLE", "comparable": False, "scored": False,
                "reason": "The image or comparison value is not a single supported quantity."}
    if image["base_uom"] != ours["base_uom"]:
        verdict = "UNIT_SIZE_OR_UOM_MISMATCH"
        reason = f"The image is {image['base_uom']}; our value is {ours['base_uom']}."
    elif image["base_unit"] == ours["base_unit"] and image["count"] == ours["count"]:
        verdict = "EXACT_KLM_MATCH"
        reason = "The image-derived unit size, UOM and pack size match our values."
    elif (image["base_uom"] in {"GM", "ML"} and image["count"] == ours["count"]
          and image["base_unit"].quantize(Decimal("1"), rounding=ROUND_HALF_UP) == ours["base_unit"]):
        verdict = "ROUNDING_EQUIVALENT_MATCH"
        reason = "The values match after the approved nearest-whole rounding rule."
    elif image["base_total"] == ours["base_total"]:
        verdict = "EQUIVALENT_TOTAL_DIFFERENT_SPLIT"
        reason = "The total quantity matches, but K and M split that total differently."
    elif image["base_unit"] == ours["base_unit"]:
        verdict = "PACK_SIZE_MISMATCH"
        reason = "The unit size matches, but the image and our value use different pack counts."
    elif image["count"] == ours["count"]:
        verdict = "UNIT_SIZE_OR_UOM_MISMATCH"
        reason = "The pack count matches, but the unit size differs."
    else:
        verdict = "TOTAL_QUANTITY_MISMATCH"
        reason = "Both the K/M representation and total quantity differ."
    return {"verdict": verdict, "comparable": True, "scored": True, "reason": reason,
            "image_normalized": {**image, "base_unit": _plain(image["base_unit"]), "base_total": _plain(image["base_total"])},
            "expected_normalized": {**ours, "base_unit": _plain(ours["base_unit"]), "base_total": _plain(ours["base_total"])}}


def interpretation(item_no: str, score: dict[str, Any]) -> dict[str, str]:
    if item_no in AUDITED:
        category, owner, action = AUDITED[item_no]
        explanation = {
            "RAW_EXCEL_LIKELY_WRONG": "The product photo contradicts the source Excel quantity; our result inherited all or part of that source value.",
            "OUR_PROPOSAL_LIKELY_WRONG": "The independent pack image does not support our proposed K/L/M.",
            "PACKAGING_DECISION_REQUIRED": "The evidence contains more than one packaging level, so M needs a business decision.",
            "DIFFERENT_MEASUREMENT_REPRESENTATION": "One side uses pieces/EA and the other uses weight or volume; both may describe the product correctly.",
        }[category]
        return {"category": category, "owner": owner, "action": action, "explanation": explanation}
    verdict = score["verdict"]
    values = {
        "EXACT_KLM_MATCH": ("CONFIRMED_EXACT", "NONE – VALUES AGREE", "No action", "Image K/L/M exactly agrees with our comparison value."),
        "ROUNDING_EQUIVALENT_MATCH": ("CONFIRMED_ROUNDING", "NONE – VALUES AGREE", "No action", "The difference is only approved nearest-whole rounding."),
        "EQUIVALENT_TOTAL_DIFFERENT_SPLIT": ("SAME_TOTAL_DIFFERENT_SPLIT", "BUSINESS POLICY", "Review K/M representation", "The total agrees, but K and M split it differently."),
        "NO_READABLE_QUANTITY": ("IMAGE_NOT_READABLE", "INSUFFICIENT IMAGE EVIDENCE", "No decision from this test", "The images did not expose a reliable quantity."),
        "NOT_COMPARABLE": ("NOT_COMPARABLE", "DIFFERENT MEASUREMENT REPRESENTATION", "Review only if required", "The image and workbook values cannot be compared safely."),
        "TECHNICAL_ERROR": ("TECHNICAL_FAILURE", "TECHNICAL PROCESS", "Retry the image check", "The image check did not complete."),
    }
    category, owner, action, explanation = values.get(verdict, ("UNRESOLVED_DIFFERENCE", "NEEDS REVIEW", "Review the evidence", score.get("reason") or "The values differ."))
    return {"category": category, "owner": owner, "action": action, "explanation": explanation}


def build(job_id: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for doc in evidence:
        group = str(doc.get("group") or "")
        if group not in {"B", "C"} or doc.get("status") != "FOUND":
            continue
        reading = dict(doc.get("photo_reading") or {})
        # C is evaluated both against its proposal and against what the download keeps.
        final_score = compare(reading, doc.get("final"))
        proposal_score = compare(reading, doc.get("suggestion")) if group == "C" and doc.get("suggestion") else None
        source = doc.get("source") or {}
        urls = source.get("image_urls") or []
        photo = reading.get("photo")
        evidence_photo = urls[photo - 1] if isinstance(photo, int) and 0 < photo <= len(urls) else None
        primary_score = proposal_score if group == "C" and proposal_score else final_score
        audit = interpretation(str(doc.get("item_no") or ""), primary_score)
        rows.append({
            "job_id": job_id, "row_number": int(doc.get("row_number") or 0), "item_no": str(doc.get("item_no") or ""),
            "group": group, "version": VERSION, "reader_version": reading.get("version") or PACK_READER_VERSION,
            "source": {"page_url": source.get("page_url"), "image_urls": urls, "evidence_photo": evidence_photo},
            "uploaded": doc.get("excel"), "proposal": doc.get("suggestion"), "final": doc.get("final"),
            "blind": {k: reading.get(k) for k in ("legible", "printed", "size", "uom", "count", "inner_count",
                                                               "outer_count", "total_count", "confidence", "photo",
                                                               "other_product_photos", "note", "model_id", "version", "images_read", "tokens")},
            "final_comparison": final_score, "proposal_comparison": proposal_score,
            "primary_comparison": primary_score,
            "interpretation": audit,
        })
    verdicts = Counter(r["primary_comparison"]["verdict"] for r in rows)
    groups: dict[str, dict[str, Any]] = {}
    for group in ("B", "C"):
        selected = [r for r in rows if r["group"] == group]
        counts = Counter(r["primary_comparison"]["verdict"] for r in selected)
        comparable = sum(1 for r in selected if r["primary_comparison"]["comparable"])
        exact = counts["EXACT_KLM_MATCH"]
        groups[group] = {"eligible": len(selected), "comparable": comparable, "exact": exact,
                         "exact_percent": round(exact * 100 / comparable, 1) if comparable else None,
                         "verdicts": dict(counts)}
    categories = Counter(r["interpretation"]["category"] for r in rows)
    return {"job_id": job_id, "kind": "BC_IMAGE", "version": VERSION, "status": "READY",
            "finished_at": datetime.now(timezone.utc), "population": len(rows), "groups": groups,
            "verdicts": dict(verdicts), "categories": dict(categories), "rows": rows}
