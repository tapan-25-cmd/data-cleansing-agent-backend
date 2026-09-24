"""Blind tests: the right answer exists before the tool runs and is hidden from it.

Group B  conversion test: complete products whose older field is in another unit;
         K/L/M hidden; the unit table's output is compared with the team's values.
Group A  seeded-error test: complete products deliberately broken in realistic ways;
         did the checker refuse to keep the broken value?
Group C  silent-text test: products whose descriptions state no size; the reader is
         shown only the text and must answer "nothing written".
The positive half of Group C is the existing AI reading test.
No test here changes a row.
"""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from app.domain.product import InputProduct
from app.services.discrepancy_service import extract_field_signals
from app.services.excel_reader import FIELD_MAP, WorkbookRow
from app.services.group_a_validator import GroupAValidator, ValidationSeverity
from app.services.normalization import clean_uom
from app.services.result_status import effective_status
from app.services.rule_engine import RuleEngine

BLIND_TEST_VERSION = "blind-test-v1"
TEXT_FIELDS = (("item_desc_eng", "item_desc_eng"), ("item_desc_local_lang", "item_desc_local"),
               ("web_description_eng", "web_description_eng"), ("web_description_chi", "web_description_chi"))

MUTATIONS: list[dict[str, str]] = [
    {"id": "unit_swapped", "name": "Unit swapped, GM written as ML or the reverse", "why": "A wrong unit changes the product's meaning entirely and is a common data-entry slip."},
    {"id": "size_is_total", "name": "Unit size replaced by the whole-pack total", "why": "The most frequent real error: 350 GM entered where 70 GM × 5 was meant."},
    {"id": "rounding_step", "name": "Unit size off by one gram or millilitre", "why": "A rounding slip that should be allowed, not flagged; measures the checker's tolerance."},
    {"id": "digit_typo", "name": "One digit of the unit size typed wrong", "why": "A plain typing mistake, for example 350 entered as 360."},
    {"id": "decimal_shift", "name": "Decimal point moved, size ten times too big", "why": "500 ML entered as 5000 ML."},
    {"id": "pack_off_by_one", "name": "Pack size off by one", "why": "6 entered where 5 was meant, on products sold in packs."},
]


def _dec(v: object) -> Decimal | None:
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def _plain(v: object) -> str:
    d = _dec(v)
    return format(d.normalize(), "f") if d is not None else str(v or "")


def _sample(items: list[dict[str, Any]], limit: int | None) -> list[dict[str, Any]]:
    """Evenly spaced and repeatable, so a smaller sample still spans the workbook."""
    if not limit or len(items) <= limit:
        return items
    step = len(items) / limit
    return [items[int(i * step)] for i in range(limit)]


def _row(item: dict[str, Any], **overrides: object) -> WorkbookRow:
    original = dict(item.get("original") or {})
    context = item.get("context") or {}
    values = {
        "row_number": int(item.get("row_number") or 0), "item_no": str(item.get("item_no") or ""),
        "department": item.get("department") or "03_Grocery 2",
        "category": context.get("category"), "subcategory": context.get("subcategory"),
        "legacy_size": original.get("legacy_size"), "legacy_uom": original.get("legacy_uom"),
        "standard_size": original.get("standard_size"), "standard_uom": original.get("standard_uom"),
        "standard_pack_size": original.get("standard_pack_size"),
        "item_brand_eng": context.get("item_brand_eng"), "item_brand_local": context.get("item_brand_local_lang"),
        "item_desc_eng": context.get("item_desc_eng"), "item_desc_local": context.get("item_desc_local_lang"),
        "web_description_eng": context.get("web_description_eng"), "web_description_chi": context.get("web_description_chi"),
    }
    values.update(overrides)
    values["raw_standard_size"] = values["standard_size"]
    values["raw_standard_uom"] = values["standard_uom"]
    values["raw_standard_pack_size"] = values["standard_pack_size"]
    product = InputProduct(**values)
    raw = {FIELD_MAP["standard_size"]: product.raw_standard_size, FIELD_MAP["standard_uom"]: product.raw_standard_uom,
           FIELD_MAP["standard_pack_size"]: product.raw_standard_pack_size}
    return WorkbookRow(row_number=product.row_number, raw=raw, product=product)


def _text_counts(item: dict[str, Any]) -> set[Decimal]:
    counts: set[Decimal] = set()
    for field, _ in TEXT_FIELDS:
        counts |= {Decimal(c.value) for c in extract_field_signals(field, (item.get("context") or {}).get(field)).counts}
    return counts


def _text_has_measurement(item: dict[str, Any]) -> bool:
    return any(extract_field_signals(f, (item.get("context") or {}).get(f)).measurements for f, _ in TEXT_FIELDS)


def clean_complete(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The answer key: complete Group A products the tool kept without a question."""
    return [x for x in items if x.get("group") == "A" and effective_status(x) in ("NO_CHANGE", "OBSERVATION_ONLY")
            and _dec((x.get("original") or {}).get("standard_size")) is not None
            and _dec((x.get("original") or {}).get("standard_pack_size")) is not None]


# ---------------------------------------------------------------- Group B: conversion
def conversion_test(items: Iterable[dict[str, Any]], engine: RuleEngine) -> dict[str, Any]:
    rows, by_type = [], defaultdict(Counter)
    for x in clean_complete(items):
        o = x["original"]
        if o.get("legacy_size") in (None, "") or not o.get("legacy_uom"):
            continue
        k, l, m = _dec(o["standard_size"]), clean_uom(o.get("standard_uom")) or str(o.get("standard_uom") or "").upper(), _dec(o["standard_pack_size"])
        legacy_uom = str(o["legacy_uom"]).strip().upper()
        needed = clean_uom(legacy_uom) != l
        proposal = engine.propose(o["legacy_size"], o["legacy_uom"])
        kind = f"{legacy_uom} → {l}"
        if proposal is None:
            verdict, got = "NO_RULE", None
        else:
            identity = proposal.source_uom == proposal.target_uom and proposal.factor == Decimal("1")
            got_size = proposal.raw_target if identity else proposal.standard_size
            got = f"{_plain(got_size)} {proposal.standard_uom}"
            if proposal.standard_uom != l:
                verdict = "DIFFERENT_UNIT"
            elif got_size == k:
                verdict = "AGREE"
            elif m and m > 1 and got_size == k * m:
                verdict = "WHOLE_PACK"
            else:
                verdict = "DIFFER"
        pack = None
        if m and m > 1:
            pack = "FOUND_IN_TEXT" if m in _text_counts(x) else "NOT_IN_TEXT"
        by_type[kind][verdict] += 1
        rows.append({"row_number": x["row_number"], "item_no": x.get("item_no"), "kind": kind, "needed_conversion": needed,
                     "legacy": f"{_plain(o['legacy_size'])} {legacy_uom}", "team": f"{_plain(k)} {l} × {_plain(m)}",
                     "tool": got, "verdict": verdict, "pack": pack,
                     "product": (x.get("context") or {}).get("item_desc_eng") or (x.get("context") or {}).get("web_description_eng") or ""})
    converted = [r for r in rows if r["needed_conversion"]]
    same = [r for r in rows if not r["needed_conversion"]]
    packs = [r for r in rows if r["pack"]]
    summary = {
        "products": len(rows),
        "needed_conversion": len(converted),
        "conversion_agree": sum(1 for r in converted if r["verdict"] == "AGREE"),
        "conversion_whole_pack": sum(1 for r in converted if r["verdict"] == "WHOLE_PACK"),
        "conversion_differ": sum(1 for r in converted if r["verdict"] in ("DIFFER", "DIFFERENT_UNIT")),
        "conversion_no_rule": sum(1 for r in converted if r["verdict"] == "NO_RULE"),
        "same_unit": len(same),
        "same_unit_agree": sum(1 for r in same if r["verdict"] == "AGREE"),
        "same_unit_whole_pack": sum(1 for r in same if r["verdict"] == "WHOLE_PACK"),
        "same_unit_differ": sum(1 for r in same if r["verdict"] in ("DIFFER", "DIFFERENT_UNIT")),
        "packs_over_one": len(packs),
        "packs_found_in_text": sum(1 for r in packs if r["pack"] == "FOUND_IN_TEXT"),
        "by_type": {k: dict(v) for k, v in by_type.items()},
    }
    return {"kind": "B", "version": BLIND_TEST_VERSION, "summary": summary, "rows": [r for r in rows if r["verdict"] != "AGREE" or r["pack"] == "NOT_IN_TEXT"]}


# ---------------------------------------------------------------- Group A: seeded errors
def _mutate(x: dict[str, Any], mutation: str) -> WorkbookRow | None:
    o = x["original"]
    k, m = _dec(o["standard_size"]), _dec(o["standard_pack_size"])
    l = clean_uom(o.get("standard_uom")) or str(o.get("standard_uom") or "").upper()
    if k is None or m is None:
        return None
    if mutation == "unit_swapped":
        if l not in ("GM", "ML"):
            return None
        return _row(x, standard_uom="ML" if l == "GM" else "GM")
    if mutation == "size_is_total":
        if m <= 1:
            return None
        return _row(x, standard_size=str(k * m))
    if mutation == "rounding_step":
        if l not in ("GM", "ML"):
            return None
        return _row(x, standard_size=str(k + 1))
    if mutation == "digit_typo":
        digits = format(k.normalize(), "f")
        if not any(ch.isdigit() and ch != "0" for ch in digits):
            return None
        for i, ch in enumerate(digits):
            if ch.isdigit() and ch != "0":
                new = digits[:i] + ("2" if ch == "1" else "1") + digits[i + 1:]
                break
        return _row(x, standard_size=new)
    if mutation == "decimal_shift":
        if l not in ("GM", "ML"):
            return None
        return _row(x, standard_size=str(k * 10))
    if mutation == "pack_off_by_one":
        if m <= 1:
            return None
        return _row(x, standard_pack_size=str(m + 1))
    return None


def seeded_error_test(items: Iterable[dict[str, Any]], validator: GroupAValidator, limit: int | None = 2000) -> dict[str, Any]:
    base = _sample(clean_complete(items), limit)
    per_mutation: dict[str, Counter[str]] = {m["id"]: Counter() for m in MUTATIONS}
    rows: list[dict[str, Any]] = []
    untouched_flagged = 0
    for x in base:
        clean = validator.validate(_row(x))
        if clean is not None and any(i.severity in (ValidationSeverity.WARNING, ValidationSeverity.ERROR) for i in clean.issues):
            untouched_flagged += 1
        for m in MUTATIONS:
            row = _mutate(x, m["id"])
            if row is None:
                continue
            result = validator.validate(row)
            issues = list(result.issues) if result is not None else []
            caught = any(i.severity in (ValidationSeverity.WARNING, ValidationSeverity.ERROR) for i in issues)
            noted = (not caught) and any(i.severity == ValidationSeverity.INFO for i in issues)
            verdict = "CAUGHT" if caught else ("NOTED_ONLY" if noted else "MISSED")
            per_mutation[m["id"]][verdict] += 1
            if verdict != "CAUGHT":
                rows.append({"row_number": x["row_number"], "item_no": x.get("item_no"), "mutation": m["id"], "verdict": verdict,
                             "team": f"{_plain(x['original']['standard_size'])} {x['original'].get('standard_uom')} × {_plain(x['original']['standard_pack_size'])}",
                             "broken": f"{_plain(row.product.standard_size)} {row.product.standard_uom} × {_plain(row.product.standard_pack_size)}",
                             "legacy": f"{_plain(x['original'].get('legacy_size'))} {x['original'].get('legacy_uom') or ''}".strip(),
                             "codes": [i.code for i in issues],
                             "product": (x.get("context") or {}).get("item_desc_eng") or ""})
    summary = {
        "products": len(base), "untouched_flagged": untouched_flagged,
        "mutations": [{**m, "tested": sum(per_mutation[m["id"]].values()), **{k: per_mutation[m["id"]].get(k, 0) for k in ("CAUGHT", "NOTED_ONLY", "MISSED")}} for m in MUTATIONS],
    }
    summary["caught"] = sum(m["CAUGHT"] for m in summary["mutations"])
    summary["tested"] = sum(m["tested"] for m in summary["mutations"])
    return {"kind": "A", "version": BLIND_TEST_VERSION, "summary": summary, "rows": rows}


# ---------------------------------------------------------------- Group C: silent text
def silent_candidates(items: Iterable[dict[str, Any]], limit: int | None = 200) -> list[dict[str, Any]]:
    """Complete products whose descriptions state no size and no count."""
    silent = [x for x in clean_complete(items) if not _text_has_measurement(x) and not _text_counts(x)]
    return _sample(silent, limit)


class SilentTextTest:
    def __init__(self, provider, *, max_concurrency: int = 5):
        self.provider = provider
        self.max_concurrency = max_concurrency

    async def run_async(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        from app.agents.provider import InferenceRequest
        sem = asyncio.Semaphore(self.max_concurrency)
        rows: list[dict[str, Any]] = []

        async def one(x: dict[str, Any]) -> None:
            c = x.get("context") or {}
            request = InferenceRequest(item_brand_eng=c.get("item_brand_eng"), item_brand_local_lang=c.get("item_brand_local_lang"),
                                       item_desc_eng=c.get("item_desc_eng"), item_desc_local_lang=c.get("item_desc_local_lang"),
                                       web_description_eng=c.get("web_description_eng"), web_description_chi=c.get("web_description_chi"),
                                       division=c.get("division"), category=c.get("category"), subcategory=c.get("subcategory"), section=c.get("section"))
            rec = {"row_number": x["row_number"], "item_no": x.get("item_no"), "product": c.get("item_desc_eng") or c.get("web_description_eng") or "",
                   "team": f"{_plain(x['original']['standard_size'])} {x['original'].get('standard_uom')} × {_plain(x['original']['standard_pack_size'])}"}
            try:
                async with sem:
                    response = await self.provider.infer(request)
                r = response.result
                rec["status"] = r.status
                rec["read"] = f"{_plain(r.measurement.value)} {r.measurement.uom}" if r.measurement else None
                rec["verdict"] = "CORRECT_SILENCE" if r.measurement is None else "INVENTED_SIZE"
                rec["rationale"] = (r.rationale or "")[:200]
            except Exception as exc:  # noqa: BLE001
                rec["verdict"] = "FAILED"; rec["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            rows.append(rec)

        await asyncio.gather(*(one(x) for x in items))
        rows.sort(key=lambda r: r["row_number"])
        counts = Counter(r["verdict"] for r in rows)
        return {"kind": "C_SILENT", "version": BLIND_TEST_VERSION,
                "summary": {"tested": len(rows), "correct_silence": counts["CORRECT_SILENCE"], "invented_size": counts["INVENTED_SIZE"], "failed": counts["FAILED"]},
                "rows": [r for r in rows if r["verdict"] != "CORRECT_SILENCE"]}

    def run(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        return asyncio.run(self.run_async(items))
