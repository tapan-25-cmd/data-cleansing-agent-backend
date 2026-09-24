"""Accuracy for stakeholders, every live product counted, no reviewer needed.

Each product of Groups A, B and C is placed in exactly one named set with a plain reason
and a per-row witness sentence. Sets have a kind:

  CONFIRMED   the product's own description states the value (an independent witness)
  CONSISTENT  another stored record agrees (the legacy field, the unit table, or the
              category's pattern); this proves consistency, not correctness, and is said so
  FLAG        sent to a person; witnesses disagree or nothing can settle it
  ALARM       sent to a person, but the text shows the review was not needed
  WRONG       the text states something else than what was kept, converted or read
  UNVERIFIED  no witness at all; shown, never scored

Score per group = (CONFIRMED + CONSISTENT) / (CONFIRMED + CONSISTENT + WRONG).
Flags are reported beside the score, never inside it; alarms are reported as the share of
flags that were not needed. Coverage = scored / products is always shown.
Read-only: nothing here changes a row.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from app.services.discrepancy_service import extract_field_signals, matches_value
from app.services.klm_reconciliation_service import KLMReconciliationService, LegacyRelationship
from app.services.result_status import effective_status
from app.services.rule_engine import RuleEngine

ACCURACY_VERSION = "accuracy-v2"
TEXT_FIELDS = ("item_desc_eng", "item_desc_local_lang", "web_description_eng", "web_description_chi")
FIELD_NAMES = {"item_desc_eng": "item description", "item_desc_local_lang": "item description (Chinese)",
               "web_description_eng": "web description", "web_description_chi": "web description (Chinese)"}
DIM = {"GM": "WEIGHT", "ML": "VOLUME"}
AMBIGUOUS_LEGACY_UNITS = {"OZ", "FZ", "FL OZ", "FLOZ"}
CONFLICT_CODES = {"DESCRIPTION_PACK_COUNT_DIFFERS", "DESCRIPTION_MEASUREMENT_MISMATCH", "BILINGUAL_DESCRIPTION_CONFLICT",
                  "PACK_COUNT_CONFLICT", "PACKAGING_HIERARCHY_AMBIGUOUS"}

KIND_LABELS = {
    "CONFIRMED": "Confirmed by the product's own words",
    "CONSISTENT": "Consistent with another record",
    "FLAG": "Sent to a person",
    "ALARM": "Sent to a person, not needed",
    "WRONG": "Contradicted by the text",
    "UNVERIFIED": "Cannot be checked",
}
SCORED_KINDS = ("CONFIRMED", "CONSISTENT", "WRONG")

SETS: dict[str, list[dict[str, str]]] = {
    "A": [
        {"id": "a_text_confirms", "kind": "CONFIRMED", "name": "The description states the values Excel has",
         "reason": "The product's own words state the size, or the piece count, that is in Excel."},
        {"id": "a_legacy_exact", "kind": "CONSISTENT", "name": "The older size field says the same",
         "reason": "The legacy field holds the same value. Usually it is the same entry copied, so this shows consistency, not proof."},
        {"id": "a_legacy_pack", "kind": "CONSISTENT", "name": "The older size field is the whole pack",
         "reason": "The legacy value equals unit size × pack size, the same pack described piece by piece."},
        {"id": "a_legacy_rounding", "kind": "CONSISTENT", "name": "The older size field agrees within label rounding",
         "reason": "After unit conversion the legacy value is within 1 gram or millilitre, or 1 percent, of Excel."},
        {"id": "a_text_disputes", "kind": "WRONG", "name": "Kept, but the description states a different size",
         "reason": "A description gives a different weight or volume than the value kept. By the agreed rule the description wins, so this counts against us."},
        {"id": "a_flag_text_supports", "kind": "FLAG", "name": "Sent to a person: legacy differs, the text supports Excel",
         "reason": "The older field disagrees, but the description states what Excel has. A person confirms; the answer is probably Excel."},
        {"id": "a_flag_conflict", "kind": "FLAG", "name": "Sent to a person: the text disagrees with Excel",
         "reason": "A description states a different count or size, or the two languages disagree. No side is picked automatically."},
        {"id": "a_flag_split", "kind": "FLAG", "name": "Sent to a person: same total, different split",
         "reason": "Legacy size × a count in the text equals Excel's total. One pack or several pieces is a business choice."},
        {"id": "a_flag_silent", "kind": "FLAG", "name": "Sent to a person: sources disagree, nothing readable settles it",
         "reason": "Legacy and Excel differ and no size or count could be read automatically from the description."},
        {"id": "a_alarm", "kind": "ALARM", "name": "Sent to a person, but the text shows Excel was right",
         "reason": "The reasoning layer quoted words from the description that state Excel's value. The review was not needed."},
        {"id": "a_unverified", "kind": "UNVERIFIED", "name": "Kept, nothing to check it against",
         "reason": "The legacy unit has no conversion and no description states a quantity."},
    ],
    "B": [
        {"id": "b_text_confirms", "kind": "CONFIRMED", "name": "The description states the converted size",
         "reason": "The product's own words state the same value the unit table produced."},
        {"id": "b_unit_respelled", "kind": "CONSISTENT", "name": "Only the unit spelling was fixed",
         "reason": "The value was already there, for example 200 G became 200 GM; the legacy field holds the same value."},
        {"id": "b_ounce_by_category", "kind": "CONSISTENT", "name": "Ounce read as weight or fluid by the category's pattern",
         "reason": "Nine in ten complete products of this category use that unit, so the ounce was read the way the category measures."},
        {"id": "b_text_disagrees", "kind": "WRONG", "name": "Converted, but the description states a different size",
         "reason": "The unit table converted the legacy value, but the product's own words give another size. The description wins."},
        {"id": "b_flag_ounce", "kind": "FLAG", "name": "Sent to a person: ounce could be weight or fluid",
         "reason": "The category holds both liquids and solids and the text does not say."},
        {"id": "b_flag_pack", "kind": "FLAG", "name": "Sent to a person: a pack count read from the text needs confirming",
         "reason": "A piece count such as 12PCS can be the pack sold or what is inside it."},
        {"id": "b_flag_text", "kind": "FLAG", "name": "Sent to a person: the text contradicts the converted value",
         "reason": "The description states a different size than the legacy field; a person decides."},
        {"id": "b_blank_unit", "kind": "FLAG", "name": "Left blank: legacy unit not in the table",
         "reason": "A unit with no agreed conversion. Blank is the right answer until one is agreed."},
        {"id": "b_unverified", "kind": "UNVERIFIED", "name": "Converted by the table, nothing to check it against",
         "reason": "The arithmetic is exact, but no description states a size and the category cannot tell a right value from a wrong one."},
    ],
    "C": [
        {"id": "c_read", "kind": "CONFIRMED", "name": "Read a size that is written in the description",
         "reason": "The value read is stated word for word in the text and passed every safety check."},
        {"id": "c_nothing_right", "kind": "CONFIRMED", "name": "Correctly left blank: nothing is written",
         "reason": "No description states a size, and a second, independent reading agreed. Leaving it blank is the correct answer."},
        {"id": "c_flag_pack", "kind": "FLAG", "name": "Sent to a person: a pack count needs confirming",
         "reason": "A piece count was read from the text; a person confirms whether it is the pack sold or the contents."},
        {"id": "c_missed", "kind": "WRONG", "name": "Left blank, but a size is written",
         "reason": "A quantity is stated in the description that the reader did not use."},
        {"id": "c_wrong_read", "kind": "WRONG", "name": "Read a value the text does not support",
         "reason": "The value read does not match what the description states."},
        {"id": "c_read_unverified", "kind": "UNVERIFIED", "name": "Read a value, not re-checked",
         "reason": "A value was read but the rule-based re-read could not find it in the text."},
        {"id": "c_blank_unverified", "kind": "UNVERIFIED", "name": "Left blank, not yet re-read",
         "reason": "The reasoning layer has not re-read this product, so its blank cannot be confirmed."},
    ],
}


def _dec(v: object) -> Decimal | None:
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def _plain(v: object) -> str:
    d = _dec(v)
    return format(d.normalize(), "f") if d is not None else str(v or "")


def _signals(context: dict[str, Any]):
    """(measurements as (signal, converted, field), counts as {value: fragment})."""
    ms, counts = [], {}
    for f in TEXT_FIELDS:
        sg = extract_field_signals(f, context.get(f))
        for m in sg.measurements:
            ms.append((m, m.source_uom != m.uom, f))
        for c in sg.counts:
            counts.setdefault(Decimal(c.value), (c.fragment, f))
    return ms, counts


def text_witness(context: dict[str, Any], size: Decimal | None, uom: str, pack: Decimal | None) -> tuple[str, str | None]:
    """('SUPPORTS' | 'DISPUTES' | 'SILENT', sentence).

    Supports when a description states the unit size, the whole pack, or, for EA, the
    piece count. Disputes when a same-dimension value is stated that matches neither.
    """
    ms, counts = _signals(context)
    if size is None:
        return ("SILENT", None) if not ms and not counts else ("SILENT", None)
    if uom == "EA":
        for value, (fragment, field) in counts.items():
            if value == size and pack in (None, Decimal("1")):
                return "SUPPORTS", f"{FIELD_NAMES[field]} says “{fragment}”: {_plain(size)} pieces"
            if pack and value == pack and size == Decimal("1"):
                return "SUPPORTS", f"{FIELD_NAMES[field]} says “{fragment}”: {_plain(pack)} pieces"
            if pack and value == size * pack:
                return "SUPPORTS", f"{FIELD_NAMES[field]} says “{fragment}”: {_plain(size * pack)} pieces in total"
        return "SILENT", None
    dim = DIM.get(uom)
    if not dim:
        return "SILENT", None
    same = [(m, c, f) for m, c, f in ms if m.dimension == dim]
    for m, c, f in same:
        if matches_value(m.value, size, converted=c):
            return "SUPPORTS", f"{FIELD_NAMES[f]} says “{m.fragment}”"
        if pack and pack > 1 and matches_value(m.value, size * pack, converted=c):
            return "SUPPORTS", f"{FIELD_NAMES[f]} says “{m.fragment}”, the whole pack"
    if same:
        m, c, f = same[0]
        return "DISPUTES", f"{FIELD_NAMES[f]} says “{m.fragment}”, not {_plain(size)} {uom}"
    return "SILENT", None


def _evidence_supports_excel(ai: dict[str, Any], size: Decimal | None, uom: str, pack: Decimal | None) -> bool:
    """An alarm needs quoted evidence that states Excel's value, not just any quote."""
    for e in ai.get("evidence") or []:
        fragment = str(e.get("fragment") or "")
        sg = extract_field_signals(str(e.get("field") or "item_desc_eng"), fragment)
        if uom == "EA":
            if any(Decimal(c.value) in {size, pack, (size or 0) * (pack or 1)} for c in sg.counts):
                return True
        else:
            dim = DIM.get(uom)
            for m in sg.measurements:
                conv = m.source_uom != m.uom
                if dim and m.dimension == dim and size is not None and (
                    matches_value(m.value, size, converted=conv) or (pack and matches_value(m.value, size * pack, converted=conv))
                ):
                    return True
    return False


class AccuracyService:
    def __init__(self, engine: RuleEngine):
        self.reconciler = KLMReconciliationService(engine)
        self.engine = engine

    def build(self, items: Iterable[dict[str, Any]], reasoning_rows: Iterable[dict[str, Any]] = ()) -> dict[str, Any]:
        items = list(items)
        reasoning = {r.get("row_number"): r for r in reasoning_rows}
        unit_by_cat: dict[str, Counter[str]] = defaultdict(Counter)
        for x in items:
            if x.get("group") == "A" and effective_status(x) in ("NO_CHANGE", "OBSERVATION_ONLY"):
                u = str((x.get("original") or {}).get("standard_uom") or "").upper()
                if u in ("GM", "ML", "EA"):
                    unit_by_cat[(x.get("context") or {}).get("category")][u] += 1
        membership: dict[str, list[int]] = defaultdict(list)
        witness: dict[str, str] = {}
        disputed_a = 0
        for x in items:
            if x.get("group") == "VALIDATION_REVIEW":
                disputed_a += 1
            set_id, why = self._classify(x, reasoning.get(x.get("row_number")), unit_by_cat)
            if set_id:
                membership[set_id].append(int(x["row_number"]))
                witness[str(x["row_number"])] = why
        reasoned_kept = sum(1 for x in items if x.get("row_number") in reasoning and effective_status(x) in ("NO_CHANGE", "OBSERVATION_ONLY", "AUTO_APPLY"))
        groups = []
        for group, sets in SETS.items():
            rows, totals = [], Counter()
            for s in sets:
                n = len(membership.get(s["id"], []))
                totals[s["kind"]] += n
                rows.append({**s, "kind_label": KIND_LABELS[s["kind"]], "products": n})
            products = sum(totals.values())
            scored = sum(totals[k] for k in SCORED_KINDS)
            right = totals["CONFIRMED"] + totals["CONSISTENT"]
            flags = totals["FLAG"] + totals["ALARM"]
            groups.append({
                "group": group, "products": products, "scored": scored, "right": right, "wrong": totals["WRONG"],
                "confirmed": totals["CONFIRMED"], "consistent": totals["CONSISTENT"],
                "flags": flags, "alarms": totals["ALARM"], "unverified": totals["UNVERIFIED"],
                "coverage_percent": round(100 * scored / products, 1) if products else None,
                "accuracy_percent": round(100 * right / scored, 1) if scored else None,
                "confirmed_percent": round(100 * totals["CONFIRMED"] / scored, 1) if scored else None,
                "sets": rows,
            })
        return {
            "version": ACCURACY_VERSION,
            "reasoning_rows_used": len(reasoning),
            "reasoning_kept_rows_checked": reasoned_kept,
            "disputed_a_rows": disputed_a,
            "kind_labels": KIND_LABELS,
            "groups": groups,
            "membership": dict(membership),
            "witness": witness,
        }

    def _classify(self, x: dict[str, Any], reasoning: dict[str, Any] | None, unit_by_cat) -> tuple[str | None, str]:
        status = effective_status(x)
        group = str(x.get("group") or "")
        if status == "SKIPPED":
            return None, ""
        codes = {f.get("code") for f in x.get("findings") or []}
        original = x.get("original") or {}
        context = x.get("context") or {}
        proposals = x.get("field_proposals") or {}
        k, m = _dec(original.get("standard_size")), _dec(original.get("standard_pack_size"))
        l = str(original.get("standard_uom") or "").upper()
        legacy_text = " ".join(str(v) for v in (original.get("legacy_size"), original.get("legacy_uom")) if v not in (None, ""))
        ai = (reasoning or {}).get("ai") or {}
        agreement = (reasoning or {}).get("agreement")
        strong_ai = bool(ai) and ai.get("confidence") == "HIGH" and bool(ai.get("evidence")) and not ai.get("needs_business_rule")

        if group in ("A", "VALIDATION_REVIEW"):
            verdict, sentence = text_witness(context, k, l, m)
            kept = status in ("NO_CHANGE", "OBSERVATION_ONLY", "AUTO_APPLY")
            if kept:
                if verdict == "DISPUTES" or "DESCRIPTION_MEASUREMENT_MISMATCH" in codes and verdict != "SUPPORTS":
                    return "a_text_disputes", sentence or "a description states a different size"
                if strong_ai and agreement == "DIFFERENT_ANSWER":
                    return "a_text_disputes", f"reasoning layer: {ai.get('explanation', '')[:160]}"
                if verdict == "SUPPORTS":
                    return "a_text_confirms", sentence or ""
                if k is not None and m is not None and l and original.get("legacy_uom"):
                    a = self.reconciler.assess_legacy(legacy_size=original.get("legacy_size"), legacy_uom=original.get("legacy_uom"),
                                                      standard_size=k, standard_uom=l, standard_pack_size=m)
                    conv = f"{_plain(a.expected_value)} {a.expected_uom}" if a.expected_value is not None else legacy_text
                    if a.relationship == LegacyRelationship.UNIT_MATCH:
                        same = str(original.get("legacy_uom") or "").upper() == l
                        return "a_legacy_exact", f"legacy {legacy_text}{'' if same else f' = {conv}'} equals Excel's {_plain(k)} {l}" + (" (same entry copied)" if same else "")
                    if a.relationship in (LegacyRelationship.TOTAL_MATCH, LegacyRelationship.TOTAL_ROUNDING_MATCH):
                        return "a_legacy_pack", f"legacy {legacy_text} = {_plain(k)} {l} × {_plain(m)}"
                    if a.relationship == LegacyRelationship.UNIT_ROUNDING_MATCH:
                        return "a_legacy_rounding", f"legacy {legacy_text} = {conv}, Excel {_plain(k)} {l}"
                if codes & {"DESCRIPTION_CONFIRMS_PIECE_COUNT", "DESCRIPTION_CONFIRMS_UNIT_SIZE"}:
                    return "a_text_confirms", "the description states Excel's values"
                return "a_unverified", f"legacy {legacy_text or 'absent'}; no readable quantity in the text"
            # sent to a person
            if strong_ai and agreement in ("AGREES_WITH_ENGINE", "KEEPS_EXCEL_AGAINST_SUGGESTION") and _evidence_supports_excel(ai, k, l, m):
                quote = "; ".join(str(e.get("fragment")) for e in ai.get("evidence") or [])
                return "a_alarm", f"reasoning layer quoted “{quote}”: Excel is right"
            if "LINKED_SIZE_AND_PACK_SUGGESTION" in codes:
                return "a_flag_split", f"legacy {legacy_text} × a count in the text = Excel's total"
            if codes & CONFLICT_CODES or verdict == "DISPUTES":
                return "a_flag_conflict", sentence or "the description disagrees with Excel"
            if verdict == "SUPPORTS":
                return "a_flag_text_supports", f"legacy {legacy_text} differs; {sentence}"
            return "a_flag_silent", f"legacy {legacy_text} vs Excel {_plain(k)} {l} × {_plain(m)}; nothing readable in the text"

        if group == "B":
            if status == "UNRESOLVED":
                return "b_blank_unit", f"legacy unit {original.get('legacy_uom')} has no conversion"
            if status == "REVIEW_REQUIRED":
                if "OUNCE_MAY_BE_FLUID" in codes:
                    return "b_flag_ounce", f"legacy {legacy_text}; category is mixed"
                if "AI_PACK_NEEDS_CONFIRMATION" in codes:
                    return "b_flag_pack", "a pack count was read from the text"
                return "b_flag_text", "the description states a different size"
            ps, pu = _dec(proposals.get("standard_size")), str(proposals.get("standard_uom") or l).upper()
            respelled = ps is None and str(x.get("reason_code") or "") == "STANDARD_FIELDS_NORMALIZATION"
            size = ps if ps is not None else k
            verdict, sentence = text_witness(context, size, pu, _dec(proposals.get("standard_pack_size")) or m)
            if verdict == "DISPUTES":
                return "b_text_disagrees", sentence or ""
            if verdict == "SUPPORTS":
                return "b_text_confirms", sentence or ""
            if respelled:
                return "b_unit_respelled", f"{_plain(k)} {original.get('standard_uom')} written as {pu}; legacy {legacy_text}"
            legacy_unit = str(original.get("legacy_uom") or "").upper()
            if legacy_unit in AMBIGUOUS_LEGACY_UNITS:
                dist = unit_by_cat.get(context.get("category"), Counter())
                n = sum(dist.values())
                if n >= 20 and dist.get(pu, 0) / n >= 0.9:
                    return "b_ounce_by_category", f"{dist.get(pu, 0)} of {n} complete products in this category use {pu}"
            if strong_ai and agreement in ("AGREES_WITH_ENGINE", "AGREES_WITH_SUGGESTION"):
                return "b_text_confirms", f"reasoning layer quoted “{'; '.join(str(e.get('fragment')) for e in ai.get('evidence') or [])}”"
            return "b_unverified", f"legacy {legacy_text} converted to {_plain(size)} {pu}; no readable size in the text"

        if group == "C":
            pack_proposed = proposals.get("standard_pack_size") not in (None, "")
            if status == "REVIEW_REQUIRED" or "AI_PACK_NEEDS_CONFIRMATION" in codes or (pack_proposed and proposals.get("standard_size") in (None, "")):
                return "c_flag_pack", "a pack count was read from the text"
            ps, pu = _dec(proposals.get("standard_size")), str(proposals.get("standard_uom") or "").upper()
            if ps is not None:
                verdict, sentence = text_witness(context, ps, pu, _dec(proposals.get("standard_pack_size")))
                if strong_ai and agreement == "DIFFERENT_ANSWER":
                    return "c_wrong_read", f"reasoning layer: {ai.get('explanation', '')[:160]}"
                if verdict == "SUPPORTS":
                    return "c_read", sentence or ""
                if verdict == "DISPUTES":
                    return "c_wrong_read", sentence or ""
                return "c_read_unverified", "the rule-based re-read could not find the value"
            ms, _ = _signals(context)
            if ms or (strong_ai and ai.get("verdict") not in (None, "CANNOT_TELL")):
                return "c_missed", (f"text says “{ms[0][0].fragment}”" if ms else f"reasoning layer: {ai.get('explanation', '')[:160]}")
            if ai and ai.get("verdict") == "CANNOT_TELL":
                return "c_nothing_right", "rule-based re-read and the reasoning layer both found nothing written"
            return "c_blank_unverified", "rule-based re-read found nothing; reasoning layer has not read it"
        return None, ""
