"""The three Release 1 accuracy measures as the client set them out (email to Dr Khoo and
Gulshan, 24 September 2026), computed on a job's current output:

  Rule accuracy    the email's measure A, on our Group B (changed by the tool): the new
                   value matches the arithmetic conversion of the legacy size
  Correctness      the email's measure B, on our Groups A and B: scored per field against
                   the Merchandising Team's cleansing once it is in the Data Lake (95% target)
  Flag precision   the email's measure C, on our Group C (raised): from each row's own
                   fields, the flag is justified

Also the email's facts about "Already correct" (consistency, not verification) and about the
flagged rows (every one has a comment). Read-only.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable

from app.services.export_service import row_comment
from app.services.guards import FLUID_OUNCE_UOM
from app.services.result_status import effective_status, outcome_group
from app.services.rule_engine import RuleEngine

OUNCES = {"OZ", "FZ", "FLOZ", "FL OZ"}
LEGACY_MISMATCH = {"SIGNIFICANT_LEGACY_SIZE_MISMATCH", "LEGACY_UOM_MISMATCH"}
FIELDS = ("standard_size", "standard_uom", "standard_pack_size")

# The client's wording, kept verbatim where the email gives it.
WORDING = {
    "intro": (
        "Three measures, reported separately, all within Release 1 scope — DFI's own data only. "
        "None of them treats the existing Standardize values as the right answer."
    ),
    "eric": (
        "The existing values are not the answer key. In his words, they “were entered by different "
        "teams at different times. They are what we expect the agent to clean, not learn from.”"
    ),
    "before_benchmark": (
        "A and C can be computed from the current file, with no review time needed from Eric's team. "
        "If the Merchandising cleansing will not be ready before the go or no-go, I suggest we decide "
        "on A and C, and report B once it lands."
    ),
    "relabel": (
        "I suggest relabelling “Already correct” as “Matches the legacy size.” Same rows, same "
        "logic, but it describes what the check actually establishes."
    ),
    "rule": {
        "letter": "A", "name": "Rule accuracy",
        "population": "the “Corrected automatically” rows",
        "test": "the proposed value matches the arithmetic conversion of the legacy size",
        "target": (
            "100% once the rounding and fluid-ounce rules are confirmed. Because this is arithmetic, "
            "any miss would be a defect to fix before launch."
        ),
        "open": (
            "Confirm the rounding and fluid-ounce rules with Eric. The agent applies one rounding rule, "
            "round to nearest, consistently. The scope does not set a rule — if truncation is "
            "preferred, the rows where the two rules differ change. The scope sets no rule for FZ."
        ),
    },
    "correctness": {
        "letter": "B", "name": "Correctness — the 95% target",
        "benchmark": (
            "the Merchandising Team's cleansing, once it is in the Data Lake. It is the model answer "
            "Eric describes, and it is DFI's own data, so it stays within scope."
        ),
        "population": (
            "the rows the agent did not mark for review or decline — “Already correct”, “Correct "
            "with a note”, and corrected and awaiting confirmation — on the rows the Merchandising "
            "cleansing covers"
        ),
        "scoring": "each field on its own, as the scope sets out. Pack size reported separately against its own baseline.",
        "target": "95% or better on unit size and on unit of measure, each on its own.",
    },
    "flags": {
        "letter": "C", "name": "Flag precision",
        "population": "the flagged rows",
        "test": (
            "from each row's own fields, confirm the flag is justified — the legacy and standard "
            "fields genuinely conflict, or no size is present. Once the Merchandising answer is "
            "available, also check how many of the flagged rows it resolves."
        ),
        "reported": "Reported separately, not in the 95%. A correct flag is a success, not a failure.",
    },
    "consistency": (
        "“Already correct” confirms consistency, not independent verification. […] the agreement adds "
        "little on its own."
    ),
    "catches": "The agent catches where the two fields disagree. […] all are left for a person to decide.",
    "comments": "Every flagged row has a comment. […] so the business gets a reason with every flag.",
}

# How our Group C reasons answer the client's test: justified because fields conflict, or
# because no size is present; not justified when the row's own text shows otherwise.
FLAG_REASONS = (
    ("conflict", True, "The legacy and standard fields genuinely conflict",
     ("c_flag_silent", "c_flag_text_supports", "c_flag_split", "c_flag_conversion")),
    ("text", True, "The description and Excel disagree",
     ("c_flag_conflict",)),
    ("ambiguous", True, "A count or an ounce could mean two things",
     ("c_flag_ounce", "c_flag_pack")),
    ("no_size", True, "No size is present to use",
     ("c_nothing_written", "c_blank_unit", "c_gap_review", "c_unusable")),
    ("other", True, "Another check found a reason",
     ("c_other",)),
    ("not_needed", False, "Not justified: the text shows Excel was right",
     ("c_alarm",)),
    ("missed", False, "Not justified: a size is written that was not used",
     ("c_missed",)),
)


def _dec(value: object) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def _plain(value: Decimal | None) -> str:
    return format(value.normalize(), "f") if value is not None else ""


def _conversion(engine: RuleEngine, size: object, uom: object, target: str | None):
    """The unrounded conversion of an old size, reading an ounce as fluid when the result is a volume."""
    if size in (None, "") or not uom:
        return None
    unit = str(uom).strip().upper()
    if target == "ML" and unit in OUNCES:
        return engine.propose(size, FLUID_OUNCE_UOM)
    return engine.propose(size, uom)


class ClientMeasures:
    def __init__(self, engine: RuleEngine):
        self.engine = engine

    def build(
        self, items: Iterable[dict[str, Any]], raised_items: Iterable[dict[str, Any]],
        membership: dict[str, list[int]],
    ) -> tuple[dict[str, Any], dict[str, list[int]], dict[str, str]]:
        items = list(items)
        lists: dict[str, list[int]] = defaultdict(list)
        witness: dict[str, str] = {}
        size = lambda ids: sum(len(membership.get(i, [])) for i in ids)  # noqa: E731

        # -- Group B: rule accuracy -------------------------------------------------
        rule_rows = Counter()
        rounding = Counter()
        pack_filled = Counter()
        changed = [x for x in items if outcome_group(x) == "B"]
        for x in changed:
            row = int(x["row_number"])
            original = x.get("original") or {}
            proposals = x.get("field_proposals") or {}
            provenance = x.get("field_provenance") or {}
            pk, pl = _dec(proposals.get("standard_size")), proposals.get("standard_uom")
            if proposals.get("standard_pack_size") is not None:
                rule = str((provenance.get("standard_pack_size") or {}).get("rule_id") or "")
                pack_filled["single" if rule == "SINGLE_ITEM_DEFAULT" else "text"] += 1
            size_method = str((provenance.get("standard_size") or {}).get("method") or "")
            size_rule = str((provenance.get("standard_size") or {}).get("rule_id") or "")
            if pk is None and pl is not None:
                key = "label"
                witness[str(row)] = f"{original.get('standard_uom')} written as {pl}, size {_plain(_dec(original.get('standard_size')))} unchanged"
            elif pk is None:
                key = "pack_only"
                witness[str(row)] = "only the pack size was filled"
            elif size_method == "AI_INFERENCE" or size_rule in {"GAP_FROM_TEXT"}:
                key = "not_conversion"
                witness[str(row)] = "the size was read from the description, not converted"
            else:
                # Converted from the legacy size, or from Excel's own value when K/L held a
                # non-standard unit (16 OZ in K/L becomes 454 GM).
                source_size, source_uom = original.get("legacy_size"), original.get("legacy_uom")
                if source_size in (None, "") or not source_uom:
                    source_size, source_uom = original.get("standard_size"), original.get("standard_uom")
                target = str(pl or original.get("standard_uom") or "").upper()
                p = _conversion(self.engine, source_size, source_uom, target)
                if p is None:
                    key = "mismatch"
                    witness[str(row)] = f"{source_size} {source_uom} has no conversion in the table"
                else:
                    raw = p.raw_target
                    nearest = raw.to_integral_value(ROUND_HALF_UP)
                    floor = raw.to_integral_value(ROUND_FLOOR)
                    fluid = str(source_uom).strip().upper() in OUNCES and p.standard_uom == "ML"
                    arithmetic = f"{source_size} {source_uom} = {_plain(raw)} {p.standard_uom}"
                    if pk != nearest:
                        key = "mismatch"
                        witness[str(row)] = f"{arithmetic}, but {_plain(pk)} was written"
                    elif fluid:
                        key = "fluid"
                        witness[str(row)] = f"{arithmetic} at US fluid ounces, rounded to nearest {_plain(nearest)}"
                    elif raw == nearest:
                        key = "exact"
                        witness[str(row)] = f"{arithmetic} exactly"
                    elif floor == nearest:
                        key = "round_same"
                        rounding["same"] += 1
                        witness[str(row)] = f"{arithmetic} → {_plain(nearest)} whether rounded or truncated"
                    else:
                        key = "round_differ"
                        rounding["differ"] += 1
                        witness[str(row)] = f"{arithmetic} → {_plain(nearest)} to nearest; truncation would give {_plain(floor)}"
            rule_rows[key] += 1
            lists[f"m_rule_{key}"].append(row)
        in_test = sum(rule_rows[k] for k in ("exact", "round_same", "round_differ", "label", "fluid", "mismatch"))
        matched = in_test - rule_rows["mismatch"]
        rule = {
            **WORDING["rule"],
            "products": len(changed),
            "tested": in_test,
            "matched": matched,
            "percent": round(100 * matched / in_test, 1) if in_test else None,
            "rows": [
                {"id": "m_rule_exact", "label": "exact conversions", "products": rule_rows["exact"]},
                {"id": "m_rule_round_same", "label": "needing rounding — the two rules agree", "products": rule_rows["round_same"]},
                {"id": "m_rule_round_differ", "label": "needing rounding — the rules differ; the agent rounded to nearest", "products": rule_rows["round_differ"]},
                {"id": "m_rule_label", "label": "change only the unit label, G to GM, with the size unchanged", "products": rule_rows["label"]},
                {"id": "m_rule_fluid", "label": "fluid-ounce rows — converted at US fluid ounces and rounded to nearest", "products": rule_rows["fluid"]},
                {"id": "m_rule_mismatch", "label": "do not match the arithmetic conversion", "products": rule_rows["mismatch"], "wrong": True},
                {"id": "m_rule_not_conversion", "label": "sizes read from the description, not converted (not part of this test)", "products": rule_rows["not_conversion"]},
                {"id": "m_rule_pack_only", "label": "only the pack size filled (not part of this test)", "products": rule_rows["pack_only"]},
            ],
            "rules_differ": rounding["differ"],
            "pack_filled": {"single_item": pack_filled["single"], "from_description": pack_filled["text"]},
        }

        # -- Group A: what "Already correct" establishes ------------------------------
        kept = [x for x in items if outcome_group(x) == "A"]
        already = [x for x in kept if effective_status(x) == "NO_CHANGE"]
        legacy = Counter()
        for x in already:
            row = int(x["row_number"])
            original = x.get("original") or {}
            k, unit = _dec(original.get("standard_size")), str(original.get("standard_uom") or "").upper()
            li, lj = _dec(original.get("legacy_size")), str(original.get("legacy_uom") or "").strip().upper()
            if li is not None and k is not None and li == k and lj == unit:
                legacy["identical"] += 1
                lists["m_a_identical"].append(row)
            p = _conversion(self.engine, original.get("legacy_size"), original.get("legacy_uom"), unit)
            if p is None or k is None:
                key = "other"
            elif p.raw_target == k:
                key = "exact"
            elif abs(p.raw_target - k) <= 1:
                key = "within_one"
            else:
                key = "other"
            if key != "other" and lj in OUNCES and unit == "ML":
                key = "fluid"
            legacy[key] += 1
            lists[f"m_a_{key}"].append(row)
        consistency = {
            "products": len(kept),
            "already_correct": len(already),
            "with_note": len(kept) - len(already),
            "exact": legacy["exact"], "within_one": legacy["within_one"], "fluid": legacy["fluid"],
            "other": legacy["other"], "identical": legacy["identical"],
            "confirmed_by_text": size(["a_text_confirms"]),
            "kept_against_text": size(["a_text_disputes"]),
            "nothing_to_compare": size(["a_unverified"]),
            "wording": WORDING["consistency"],
            "relabel": WORDING["relabel"],
        }

        # -- Groups A + B: correctness against the benchmark -----------------------------
        correctness = {
            **WORDING["correctness"],
            "status": "WAITING_FOR_BENCHMARK",
            "products": len(kept) + len(changed),
            "population_rows": [
                {"label": "Already correct", "products": len(already)},
                {"label": "Correct with a note", "products": len(kept) - len(already)},
                {"label": "Corrected automatically", "products": len(changed)},
            ],
        }

        # -- Group C: flag precision ----------------------------------------------------
        raised = list(raised_items)
        reasons = []
        for key, justified, label, ids in FLAG_REASONS:
            rows = [r for i in ids for r in membership.get(i, [])]
            lists[f"m_flag_{key}"] = rows
            reasons.append({"id": f"m_flag_{key}", "label": label, "justified": justified, "products": len(rows)})
        justified = sum(r["products"] for r in reasons if r["justified"])
        flagged = sum(r["products"] for r in reasons)
        review = [x for x in raised if effective_status(x) == "REVIEW_REQUIRED"]
        has_proposal = lambda x: any((x.get("field_proposals") or {}).get(f) is not None for f in FIELDS)  # noqa: E731
        mismatch = [x for x in review if {f.get("code") for f in x.get("findings") or []} & LEGACY_MISMATCH]
        blank = [x for x in raised if not row_comment(x).strip()]
        flags = {
            **WORDING["flags"],
            "products": flagged,
            "justified": justified,
            "percent": round(100 * justified / flagged, 1) if flagged else None,
            "reasons": reasons,
            "needs_review": len(review),
            "could_not_determine": sum(1 for x in raised if effective_status(x) == "UNRESOLVED"),
            "invalid": sum(1 for x in raised if effective_status(x) == "INVALID"),
            "legacy_mismatch": len(mismatch),
            "legacy_mismatch_with_proposal": sum(1 for x in mismatch if has_proposal(x)),
            "with_comment": len(raised) - len(blank),
            "blank_comment": len(blank),
            "with_proposal": sum(1 for x in raised if has_proposal(x)),
            "catches": WORDING["catches"],
            "comments": WORDING["comments"],
            "merchandising_resolves": None,
        }
        report = {
            "intro": WORDING["intro"], "eric": WORDING["eric"], "before_benchmark": WORDING["before_benchmark"],
            "rule_accuracy": rule, "consistency": consistency, "correctness": correctness, "flag_precision": flags,
        }
        return report, dict(lists), witness
