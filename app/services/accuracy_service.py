"""Accuracy for stakeholders, every live product counted, no reviewer needed.

Products are judged by their outcome group, each against its own question:

  A  No change            was keeping the values right?
  B  Changed by the tool  was the change right?
  C  Raised for a person  was raising it right?

Each product sits in exactly one named set with a plain reason and a per-row witness
sentence. Sets have a kind:

  CONFIRMED   the product's own description states the value (an independent witness)
  CONSISTENT  another stored record agrees (the legacy field, the unit table, or the
              category's pattern); this proves consistency, not correctness, and is said so
  FLAG        raised for a person for a reason the data bears out (Group C: right)
  ALARM       raised, but the text shows it was not needed (Group C: against)
  WRONG       the text states something else than what was kept, changed or left
  UNVERIFIED  no witness at all; shown, never scored

A and B: accuracy = (CONFIRMED + CONSISTENT) / (CONFIRMED + CONSISTENT + WRONG), with
coverage = scored / products always shown. C: accuracy = FLAG / (FLAG + ALARM + WRONG),
every raised product is judged. Read-only: nothing here changes a row.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from app.services.discrepancy_service import extract_field_signals, matches_value
from app.services.klm_reconciliation_service import KLMReconciliationService, LegacyRelationship
from app.services.result_status import GROUP_NAMES, effective_status, outcome_group, route_label, route_of
from app.services.rule_engine import RuleEngine

ACCURACY_VERSION = "accuracy-v4"
TEXT_FIELDS = ("item_desc_eng", "item_desc_local_lang", "web_description_eng", "web_description_chi")
FIELD_NAMES = {"item_desc_eng": "item description", "item_desc_local_lang": "item description (Chinese)",
               "web_description_eng": "web description", "web_description_chi": "web description (Chinese)"}
DIM = {"GM": "WEIGHT", "ML": "VOLUME"}
AMBIGUOUS_LEGACY_UNITS = {"OZ", "FZ", "FL OZ", "FLOZ"}
CONFLICT_CODES = {"DESCRIPTION_PACK_COUNT_DIFFERS", "DESCRIPTION_SIZE_DIFFERS", "BILINGUAL_DESCRIPTION_CONFLICT",
                  "PACK_COUNT_CONFLICT", "PACKAGING_HIERARCHY_AMBIGUOUS"}
QUESTIONS = {
    "A": "Was keeping the values right?",
    "B": "Was the change right?",
    "C": "Was raising it right?",
}

KIND_LABELS = {
    "CONFIRMED": "Confirmed by the product's own words",
    "CONSISTENT": "Consistent with another record",
    "FLAG": "Raised for a good reason",
    "ALARM": "Raised, not needed",
    "WRONG": "Contradicted by the text",
    "UNVERIFIED": "Cannot be checked",
}
SCORED_KINDS = ("CONFIRMED", "CONSISTENT", "WRONG")
RAISED_SCORED_KINDS = ("FLAG", "ALARM", "WRONG")

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
        {"id": "a_unverified", "kind": "UNVERIFIED", "name": "Kept, nothing to check it against",
         "reason": "The legacy unit has no conversion and no description states a quantity."},
    ],
    "B": [
        {"id": "b_text_confirms", "kind": "CONFIRMED", "name": "The description states the new value",
         "reason": "The value was worked out from the older field, and the product's own words state the same value."},
        {"id": "b_read_confirmed", "kind": "CONFIRMED", "name": "Read from the description, and written there",
         "reason": "The value was read from the text and is stated there word for word; it passed every safety check."},
        {"id": "b_unit_respelled", "kind": "CONSISTENT", "name": "Only the unit spelling was fixed",
         "reason": "The value was already there, for example 200 G became 200 GM; the legacy field holds the same value."},
        {"id": "b_gap_matches", "kind": "CONSISTENT", "name": "Missing unit filled, the older field matches the size",
         "reason": "Excel had the size but not the unit. The older field states the same size in a unit, so that unit was written."},
        {"id": "b_ounce_by_category", "kind": "CONSISTENT", "name": "Ounce read as weight or fluid by the category's pattern",
         "reason": "Nine in ten complete products of this category use that unit, so the ounce was read the way the category measures."},
        {"id": "b_text_disagrees", "kind": "WRONG", "name": "Changed, but the description states a different size",
         "reason": "The value came from the older field, but the product's own words give another size. The description wins."},
        {"id": "b_read_wrong", "kind": "WRONG", "name": "Read a value the text does not support",
         "reason": "The value read does not match what the description states."},
        {"id": "b_unverified", "kind": "UNVERIFIED", "name": "Converted by the table, nothing to check it against",
         "reason": "The arithmetic is exact, but no description states a size and the category cannot tell a right value from a wrong one."},
        {"id": "b_read_unverified", "kind": "UNVERIFIED", "name": "Read a value, not re-checked",
         "reason": "A value was read but the rule-based re-read could not find it in the text."},
    ],
    "C": [
        {"id": "c_flag_conflict", "kind": "FLAG", "name": "The description disagrees with Excel",
         "reason": "A description states a different size or count than Excel, or the two languages disagree. No side is picked automatically."},
        {"id": "c_flag_silent", "kind": "FLAG", "name": "Older field and Excel disagree, nothing readable settles it",
         "reason": "Two records give different values and no size or count could be read from the description."},
        {"id": "c_flag_split", "kind": "FLAG", "name": "Same total, different split",
         "reason": "Legacy size × a count in the text equals Excel's total. One pack or several pieces is a business choice."},
        {"id": "c_flag_text_supports", "kind": "FLAG", "name": "Older field differs, the text supports Excel",
         "reason": "The description states what Excel has; the review is asked only because the older field disagrees."},
        {"id": "c_flag_ounce", "kind": "FLAG", "name": "An ounce could be weight or fluid",
         "reason": "The category holds both liquids and solids and the text does not say."},
        {"id": "c_flag_pack", "kind": "FLAG", "name": "A pack count read from the text needs confirming",
         "reason": "A piece count such as 12PCS can be the pack sold or what is inside it."},
        {"id": "c_flag_conversion", "kind": "FLAG", "name": "The converted value needs a person",
         "reason": "The older field was converted, but the description or the pack disagrees with it; a person decides."},
        {"id": "c_blank_unit", "kind": "FLAG", "name": "Older unit not in the table",
         "reason": "A unit with no agreed conversion. Blank is the right answer until one is agreed."},
        {"id": "c_nothing_written", "kind": "FLAG", "name": "Nothing written to read",
         "reason": "No description states a size, and a rule-based re-read of the same six fields finds none either."},
        {"id": "c_gap_review", "kind": "FLAG", "name": "Half-filled row: a missing value was not found",
         "reason": "The row had some of size, unit and pack. What could be found was filled; the rest was only suggested, or is written nowhere."},
        {"id": "c_unusable", "kind": "FLAG", "name": "A value in Excel cannot be used",
         "reason": "Text where a number should be, or a unit that is in no table. The row can be neither checked nor completed."},
        {"id": "c_other", "kind": "FLAG", "name": "Raised for another checked reason",
         "reason": "A check found a reason for a person to look; the comment on the row names it."},
        {"id": "c_alarm", "kind": "ALARM", "name": "Raised, but the text shows Excel was right",
         "reason": "The reasoning layer quoted words from the description that state Excel's value. The review was not needed."},
        {"id": "c_missed", "kind": "WRONG", "name": "Left blank, but a size is written",
         "reason": "A quantity is stated in the description that the reader did not use."},
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
            if route_of(x) == "A" and effective_status(x) in ("NO_CHANGE", "OBSERVATION_ONLY"):
                u = str((x.get("original") or {}).get("standard_uom") or "").upper()
                if u in ("GM", "ML", "EA"):
                    unit_by_cat[(x.get("context") or {}).get("category")][u] += 1
        membership: dict[str, list[int]] = defaultdict(list)
        witness: dict[str, str] = {}
        routes: dict[str, Counter[str]] = defaultdict(Counter)
        for x in items:
            if effective_status(x) == "SKIPPED":
                continue
            group = outcome_group(x)
            routes[group][route_label(x)] += 1
            set_id, why = self._classify(x, group, reasoning.get(x.get("row_number")), unit_by_cat)
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
            raised = group == "C"
            scored = sum(totals[k] for k in (RAISED_SCORED_KINDS if raised else SCORED_KINDS))
            right = totals["FLAG"] if raised else totals["CONFIRMED"] + totals["CONSISTENT"]
            groups.append({
                "group": group, "name": GROUP_NAMES[group], "question": QUESTIONS[group],
                "products": products, "scored": scored, "right": right, "wrong": totals["WRONG"],
                "confirmed": totals["CONFIRMED"], "consistent": totals["CONSISTENT"],
                "flags": totals["FLAG"], "alarms": totals["ALARM"], "unverified": totals["UNVERIFIED"],
                "coverage_percent": round(100 * scored / products, 1) if products else None,
                "accuracy_percent": round(100 * right / scored, 1) if scored else None,
                "confirmed_percent": round(100 * totals["CONFIRMED"] / scored, 1) if scored and not raised else None,
                "routes": [{"route": name, "products": count} for name, count in routes[group].most_common()],
                "sets": rows,
            })
        return {
            "version": ACCURACY_VERSION,
            "reasoning_rows_used": len(reasoning),
            "reasoning_kept_rows_checked": reasoned_kept,
            "kind_labels": KIND_LABELS,
            "groups": groups,
            "membership": dict(membership),
            "witness": witness,
        }

    def _classify(self, x: dict[str, Any], group: str, reasoning: dict[str, Any] | None, unit_by_cat) -> tuple[str, str]:
        status = effective_status(x)
        route = route_of(x)
        codes = {f.get("code") for f in x.get("findings") or []}
        original = x.get("original") or {}
        context = x.get("context") or {}
        proposals = x.get("field_proposals") or {}
        provenance = x.get("field_provenance") or {}
        k, m = _dec(original.get("standard_size")), _dec(original.get("standard_pack_size"))
        l = str(original.get("standard_uom") or "").upper()
        legacy_text = " ".join(str(v) for v in (original.get("legacy_size"), original.get("legacy_uom")) if v not in (None, ""))
        ai = (reasoning or {}).get("ai") or {}
        agreement = (reasoning or {}).get("agreement")
        strong_ai = bool(ai) and ai.get("confidence") == "HIGH" and bool(ai.get("evidence")) and not ai.get("needs_business_rule")
        quote = "; ".join(str(e.get("fragment")) for e in ai.get("evidence") or [])

        # ---- A: nothing changed. Was keeping the values right?
        if group == "A":
            if k is None:
                return "a_unverified", "no size was written and none was found"
            verdict, sentence = text_witness(context, k, l, m)
            if verdict == "DISPUTES":
                return "a_text_disputes", sentence or "a description states a different size"
            if strong_ai and agreement == "DIFFERENT_ANSWER":
                return "a_text_disputes", f"reasoning layer: {ai.get('explanation', '')[:160]}"
            if verdict == "SUPPORTS":
                return "a_text_confirms", sentence or ""
            if m is not None and l and original.get("legacy_uom"):
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

        # ---- B: the tool changed K, L or M. Was the change right?
        if group == "B":
            size = _dec(proposals.get("standard_size")) if proposals.get("standard_size") is not None else k
            unit = str(proposals.get("standard_uom") or l).upper()
            pack = _dec(proposals.get("standard_pack_size")) or m
            read = route == "C" or any((provenance.get(f) or {}).get("method") == "AI_INFERENCE" for f in ("standard_size", "standard_uom"))
            verdict, sentence = text_witness(context, size, unit, pack)
            if read:
                if strong_ai and agreement == "DIFFERENT_ANSWER":
                    return "b_read_wrong", f"reasoning layer: {ai.get('explanation', '')[:160]}"
                if verdict == "SUPPORTS":
                    return "b_read_confirmed", sentence or ""
                if verdict == "DISPUTES":
                    return "b_read_wrong", sentence or ""
                return "b_read_unverified", "the rule-based re-read could not find the value"
            if verdict == "DISPUTES":
                return "b_text_disagrees", sentence or ""
            if verdict == "SUPPORTS":
                return "b_text_confirms", sentence or ""
            rules = {str((provenance.get(f) or {}).get("rule_id") or "") for f in ("standard_size", "standard_uom", "standard_pack_size")
                     if proposals.get(f) is not None}
            if "GAP_PACK_FROM_TEXT" in rules and proposals.get("standard_size") is None and proposals.get("standard_uom") is None:
                evidence = str((provenance.get("standard_pack_size") or {}).get("evidence_text") or "the description states the count")
                return "b_read_confirmed", evidence
            if str(x.get("reason_code") or "") == "STANDARD_FIELDS_NORMALIZATION" and proposals.get("standard_size") is None:
                return "b_unit_respelled", f"{_plain(k)} {original.get('standard_uom')} written as {unit}; legacy {legacy_text}"
            if route == "INCOMPLETE" and proposals.get("standard_uom") is not None and proposals.get("standard_size") is None \
                    and (provenance.get("standard_uom") or {}).get("rule_id") == "GAP_FROM_LEGACY":
                return "b_gap_matches", str((provenance.get("standard_uom") or {}).get("evidence_text") or f"legacy {legacy_text}")
            legacy_unit = str(original.get("legacy_uom") or "").upper()
            if legacy_unit in AMBIGUOUS_LEGACY_UNITS:
                dist = unit_by_cat.get(context.get("category"), Counter())
                n = sum(dist.values())
                if n >= 20 and dist.get(unit, 0) / n >= 0.9:
                    return "b_ounce_by_category", f"{dist.get(unit, 0)} of {n} complete products in this category use {unit}"
            if strong_ai and agreement in ("AGREES_WITH_ENGINE", "AGREES_WITH_SUGGESTION"):
                return "b_text_confirms", f"reasoning layer quoted “{quote}”"
            return "b_unverified", f"legacy {legacy_text or 'absent'} gives {_plain(size)} {unit}; no readable size in the text"

        # ---- C: raised for a person. Was raising it right?
        if status == "INVALID" or "UNUSABLE_VALUE" in codes:
            return "c_unusable", f"Excel has {_plain(original.get('standard_size')) or 'no size'} {original.get('standard_uom') or ''}".strip()
        if route in ("A", "VALIDATION_REVIEW"):
            verdict, sentence = text_witness(context, k, l, m)
            if strong_ai and agreement in ("AGREES_WITH_ENGINE", "KEEPS_EXCEL_AGAINST_SUGGESTION") and _evidence_supports_excel(ai, k, l, m):
                return "c_alarm", f"reasoning layer quoted “{quote}”: Excel is right"
            if "LINKED_SIZE_AND_PACK_SUGGESTION" in codes:
                return "c_flag_split", f"legacy {legacy_text} × a count in the text = Excel's total"
            if codes & CONFLICT_CODES or verdict == "DISPUTES":
                return "c_flag_conflict", sentence or "the description disagrees with Excel"
            if verdict == "SUPPORTS":
                return "c_flag_text_supports", f"legacy {legacy_text} differs; {sentence}"
            if codes & {"SIGNIFICANT_LEGACY_SIZE_MISMATCH", "LEGACY_UOM_MISMATCH"}:
                return "c_flag_silent", f"legacy {legacy_text} vs Excel {_plain(k)} {l} × {_plain(m)}; nothing readable in the text"
            return "c_other", "; ".join(sorted(str(c) for c in codes if c)) or "raised by a check"
        if route == "B":
            if status == "UNRESOLVED":
                return "c_blank_unit", f"legacy unit {original.get('legacy_uom')} has no conversion"
            if "OUNCE_MAY_BE_FLUID" in codes:
                return "c_flag_ounce", f"legacy {legacy_text}; category is mixed"
            if "AI_PACK_NEEDS_CONFIRMATION" in codes:
                return "c_flag_pack", "a pack count was read from the text"
            return "c_flag_conversion", f"legacy {legacy_text}; the text or the pack disagrees"
        if route == "C":
            pack_proposed = proposals.get("standard_pack_size") not in (None, "")
            if status == "REVIEW_REQUIRED" or "AI_PACK_NEEDS_CONFIRMATION" in codes or pack_proposed:
                return "c_flag_pack", "a pack count was read from the text"
            ms, _ = _signals(context)
            if ms or (strong_ai and ai.get("verdict") not in (None, "CANNOT_TELL")):
                return "c_missed", (f"text says “{ms[0][0].fragment}”" if ms else f"reasoning layer: {ai.get('explanation', '')[:160]}")
            if ai and ai.get("verdict") == "CANNOT_TELL":
                return "c_nothing_written", "rule-based re-read found nothing written; the reasoning layer agreed"
            return "c_nothing_written", "rule-based re-read of all six fields found nothing written"
        if route == "INCOMPLETE":
            missing = [str(f.get("human_reason") or "") for f in x.get("findings") or [] if f.get("code") in {"GAP_NOT_FOUND", "GAP_SUGGESTED"}]
            if missing:
                return "c_gap_review", missing[0][:200]
            verdict, sentence = text_witness(context, k, l, m)
            if codes & CONFLICT_CODES or verdict == "DISPUTES":
                return "c_flag_conflict", sentence or "the description disagrees with the completed values"
        return "c_other", "; ".join(sorted(str(c) for c in codes if c)) or "raised by a check"
