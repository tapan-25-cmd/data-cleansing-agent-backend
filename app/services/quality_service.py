"""Client-facing quality report for one processed workbook.

Two honest parts, never blended:

* **Agreement** – a blind test. For products whose K/L/M were already entered and
  validated (Group A), the answers are hidden, the agent works the value out again
  from the remaining source data, and the result is compared with what the team
  entered. A match counts toward the agreement figure.
* **Open questions** – a disagreement is *not* an agent error: either side may be
  wrong. It is listed as a business decision until someone decides, and only then
  counts for or against the agent.

Nothing here calls a model. The report is computed from stored result items, so it
also works for jobs processed before this service existed.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from app.agents.versions import AGENT_VERSION, PROMPT_VERSION
from app.domain.product import InputProduct
from app.rules.registry import RuleRegistry
from app.services import guards
from app.services.category_profile import CategoryProfile, CategoryView, levels_of
from app.services.discrepancy_service import (
    within_conversion_tolerance,
    extract_field_signals,
    text_check,
)
from app.services.pack_size_service import PackSizeService, PackStatus
from app.services.result_status import effective_status
from app.services.rule_engine import RuleEngine


QUALITY_REPORT_VERSION = "quality-report-v5"
DECISIONS_PATH = (
    Path(__file__).resolve().parent.parent / "evaluations" / "business_decisions.v1.yaml"
)
MAX_EXAMPLES = 25

DESCRIPTION_FIELDS = (
    ("item_desc_eng", "item_desc_eng"),
    ("item_desc_local_lang", "item_desc_local"),
    ("web_description_eng", "web_description_eng"),
    ("web_description_chi", "web_description_chi"),
)


@dataclass(frozen=True)
class BlindInput:
    """Everything the blind test may see. The answer key (K/L/M) has no field here,
    so a prediction cannot depend on it (FR-39)."""

    legacy_size: object | None
    legacy_uom: str | None
    item_desc_eng: str | None
    item_desc_local: str | None
    web_description_eng: str | None
    web_description_chi: str | None

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> "BlindInput":
        original = item.get("original") or {}
        context = item.get("context") or {}
        return cls(
            legacy_size=original.get("legacy_size"),
            legacy_uom=original.get("legacy_uom"),
            item_desc_eng=context.get("item_desc_eng"),
            item_desc_local=context.get("item_desc_local_lang"),
            web_description_eng=context.get("web_description_eng"),
            web_description_chi=context.get("web_description_chi"),
        )


class BusinessDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    why_it_matters: str
    options: list[str]
    unlocks: str
    counts_from: str
    status: Literal["OPEN", "DECIDED"] = "OPEN"
    # For decisions that settle blind-test disagreements: who was right.
    resolution: Literal["AGENT_CORRECT", "EXCEL_CORRECT"] | None = None
    decided_note: str | None = None


@lru_cache(maxsize=1)
def load_business_decisions() -> tuple[str, tuple[BusinessDecision, ...]]:
    payload = yaml.safe_load(DECISIONS_PATH.read_text(encoding="utf-8"))
    decisions = tuple(BusinessDecision.model_validate(row) for row in payload["decisions"])
    return str(payload["version"]), decisions


def _decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None


def _plain(value: object) -> str:
    number = _decimal(value)
    if number is None:
        return "" if value is None else str(value)
    return format(number.normalize(), "f")


def _label(item: dict[str, Any]) -> str:
    context = item.get("context") or {}
    return str(context.get("item_desc_eng") or context.get("web_description_eng") or "")


def compare_size(
    size: Decimal, uom: str, converted: bool,
    answer_size: Decimal, answer_uom: str,
    *, given: str, unit_source: str,
) -> tuple[bool, str, str]:
    """(agrees, kind, plain-language note) for an agent size/unit against Excel."""
    if uom != answer_uom:
        return False, "UNIT", (
            f"{unit_source} is a different kind of unit from the {answer_uom} in Excel."
        )
    if size == answer_size:
        return True, "SIZE", "Same value."
    if converted and within_conversion_tolerance(size, answer_size):
        return True, "SIZE", "Same value after rounding."
    return False, "SIZE", (
        f"{given} works out to {_plain(size)} {uom}, but Excel has "
        f"{_plain(answer_size)} {answer_uom}."
    )


def answer_key(item: dict[str, Any]) -> tuple[Decimal | None, str, Decimal | None]:
    original = item.get("original") or {}
    return (
        _decimal(original.get("standard_size")),
        str(original.get("standard_uom") or "").strip().upper(),
        _decimal(original.get("standard_pack_size")),
    )


def text_states(item: dict[str, Any], size: Decimal | None, uom: str, pack: Decimal | None) -> bool:
    """Does any description field state this size (per piece or as the pack total)?"""
    if size is None:
        return False
    blind = BlindInput.from_item(item)
    signals = [
        extract_field_signals(field, getattr(blind, attribute))
        for field, attribute in DESCRIPTION_FIELDS
    ]
    return text_check(signals, size, uom, pack)[0] == "CONFIRMED"


def is_ai_reading_eligible(item: dict[str, Any]) -> bool:
    """An already-completed product whose description states a size in words."""
    if item.get("group") != "A":
        return False
    blind = BlindInput.from_item(item)
    return any(
        extract_field_signals(field, getattr(blind, attribute)).measurements
        for field, attribute in DESCRIPTION_FIELDS
    )


# Below this many reviewer verdicts the figure is too thin to headline.
MIN_VERIFIED_FOR_HEADLINE = 30

# What the agent produced, in the order a stakeholder reads it.
#   text_checkable: the result came from legacy/entered data, so the product text is
#   an *independent* witness. A result read from the text itself is not checkable
#   against that same text, and only a reviewer can confirm it.
RESULT_KINDS: tuple[tuple[str, str, str, bool], ...] = (
    ("CONVERTED", "ACTION", "Sizes and units it converted to the standard", True),
    ("FILLED", "ACTION", "Blank sizes it filled in from the legacy data", True),
    ("TIDIED", "ACTION", "Unit spellings it tidied (for example G → GM)", True),
    ("PACK", "ACTION", "Pack sizes it filled in from the description", False),
    ("AI_SIZE", "ACTION", "Sizes the AI read from the description", False),
    ("SUGGESTED_SIZE", "SUGGESTION", "Corrections it suggested for a person to approve", True),
    ("FLAGGED", "SUGGESTION", "Products it flagged for a person to check", False),
    ("LEFT_BLANK", "SUGGESTION", "Products it left blank rather than guess", False),
)


def result_kinds(item: dict[str, Any]) -> list[str]:
    """Every kind of result the agent produced for one product."""
    group = item.get("group")
    proposals = item.get("field_proposals") or {}
    original = item.get("original") or {}
    has_size = proposals.get("standard_size") is not None or proposals.get("standard_uom") is not None
    kinds: list[str] = []
    tidied = (item.get("rule") or {}).get("rule_id") == "STANDARD_FIELDS_CANONICALIZATION"
    if group == "B" and tidied:
        kinds.append("TIDIED")
    elif group == "B" and has_size:
        kinds.append("FILLED" if original.get("standard_size") is None else "CONVERTED")
    elif group == "C" and has_size:
        kinds.append("AI_SIZE")
    elif group == "C":
        kinds.append("LEFT_BLANK")
    elif has_size:
        kinds.append("SUGGESTED_SIZE")
    elif item.get("application_policy") == "REVIEW_REQUIRED":
        kinds.append("FLAGGED")
    if proposals.get("standard_pack_size") is not None and not tidied:
        kinds.append("PACK")
    return kinds


def reviewer_verdict(item: dict[str, Any], kind: str) -> bool | None:
    """True = the result was right, False = wrong, None = nobody has checked it."""
    verdict = (item.get("verification") or {}).get("verdict")
    if verdict in {"CORRECT", "WRONG"}:
        return verdict == "CORRECT"
    # Approving a suggestion says it was right; replacing it says it was not.
    if kind == "SUGGESTED_SIZE":
        status = (item.get("review") or {}).get("overall_status")
        if status == "APPROVED":
            return True
        if status in {"REJECTED", "OVERRIDDEN"}:
            return False
    return None


ASK_A_PERSON = "ASK_A_PERSON"

# guard code -> (what it checks, what it does), in the order shown to stakeholders
SAFETY_CHECKS: dict[str, tuple[str, str]] = {
    "OUNCE_READ_AS_FLUID": (
        "Ounces on a liquid are fluid ounces", "Converted to millilitres instead of grams"),
    "OUNCE_MAY_BE_FLUID": (
        "Ounces could be a weight or a volume", "Sent to a person with both values"),
    "TEXT_CONTRADICTS_RESULT": (
        "The product's own description disagrees with the value", "Stopped and sent to a person"),
    "LEGACY_MAY_BE_PACK_TOTAL": (
        "The legacy size looks like the whole pack, not one piece", "Stopped and sent to a person"),
    "AI_MEASUREMENT_NOT_PRODUCT_SIZE": (
        "The AI read a number that is not an amount of product (a capacity, a grade)",
        "Refused; left blank"),
    "AI_PACK_NEEDS_CONFIRMATION": (
        "The AI read a pack size from loose wording (12PCS, 1PC, 4'S)",
        "Suggested, and sent to a person to confirm"),
    "SIZE_OUTSIDE_CATEGORY_RANGE": (
        "A size read from text is far outside what is normal for the category",
        "Stopped and sent to a person"),
    "SIZE_UNUSUAL_FOR_CATEGORY": (
        "A converted size is unusually large or small for the category", "Applied, with a note"),
    "COUNT_IN_MEASURED_CATEGORY": (
        "Only a count is available where products usually carry a weight", "Applied, with a note"),
}

AI_READING_KEY = "ai_description_reading"
AI_READING_CAPABILITY = "Reads the size from the product description (AI)"
AI_READING_HOW = (
    "We showed the AI only the description text of products whose size your team had "
    "already entered, and compared what it read with that size."
)


class QualityService:
    def __init__(self, registry: RuleRegistry):
        # Same nearest-whole policy the pipeline uses for unit conversion.
        self.rule_engine = RuleEngine(registry, 0)
        self.pack_size_service = PackSizeService()

    # ---- blind predictions: BlindInput in, prediction out -------------------

    def predict_size_and_unit(
        self, blind: BlindInput, view: CategoryView | None = None,
    ) -> tuple[Decimal, str, bool] | None:
        """(size, unit, converted) from the legacy size/unit, read as production reads it.

        ``view`` is what the *other* validated rows say about this product's category; it
        decides whether an ``OZ`` is a fluid ounce. Returns ``ASK_A_PERSON`` as the unit
        when production would send the row to review instead of answering."""
        if blind.legacy_size is None or not blind.legacy_uom:
            return None
        proposal = self.rule_engine.propose(blind.legacy_size, blind.legacy_uom)
        if proposal is None:
            return None
        reading = guards.ounce_reading(blind.legacy_uom, view) if view else "WEIGHT"
        if reading == "REVIEW":
            return proposal.standard_size, ASK_A_PERSON, True
        if reading == "VOLUME":
            proposal = self.rule_engine.propose(blind.legacy_size, guards.FLUID_OUNCE_UOM) or proposal
        identity = proposal.source_uom == proposal.target_uom and proposal.factor == Decimal("1")
        size = proposal.raw_target if identity else proposal.standard_size
        return size, proposal.standard_uom, not identity

    def predict_pack_size(self, blind: BlindInput) -> tuple[Decimal, str] | None:
        """(pack size, literal evidence) from description text only."""
        product = InputProduct(
            row_number=0, item_no="",
            item_desc_eng=blind.item_desc_eng, item_desc_local=blind.item_desc_local,
            web_description_eng=blind.web_description_eng,
            web_description_chi=blind.web_description_chi,
        )
        assessment = self.pack_size_service.assess(product)
        if assessment.status != PackStatus.DETERMINISTIC_PROPOSAL or assessment.pack_size is None:
            return None
        return assessment.pack_size, assessment.evidence.fragment if assessment.evidence else ""

    # ---- report ---------------------------------------------------------------

    def build_report(self, job: dict[str, Any], items: Iterable[dict[str, Any]]) -> dict[str, Any]:
        items = list(items)
        decisions_version, decisions = load_business_decisions()
        resolutions = {
            decision.counts_from: decision.resolution
            for decision in decisions
            if decision.status == "DECIDED" and decision.resolution
        }

        unit = CapabilityScore(
            "unit_conversion",
            "Converts size and unit to the standard (for example 1 KG → 1000 GM)",
            "We hid the size and unit your team entered, let the agent convert the legacy "
            "size and unit, and compared the two.",
        )
        pack = CapabilityScore(
            "pack_size",
            "Works out the pack size from the product description",
            "We hid the pack size your team entered, let the agent read the pack from the "
            "description text, and compared the two.",
        )
        profile = CategoryProfile.from_items(items)
        counters: Counter[str] = Counter()
        first_item: dict[str, dict[str, Any]] = {}
        ai_reading_eligible = 0

        def count(key: str, item: dict[str, Any]) -> None:
            counters[key] += 1
            first_item.setdefault(key, item)

        for item in items:
            for code in {finding.get("code") for finding in item.get("findings") or []}:
                count(f"finding:{code}", item)
            if item.get("reason_code"):
                count(f"reason:{item['reason_code']}", item)
            group = item.get("group")
            pack_status = (item.get("pack_result") or {}).get("status")
            if group == "B" and pack_status in {"AGENT_PROPOSAL", "AGENT_DECLINED", "AGENT_ERROR", "AGENT_DISABLED"}:
                count("pack_clue_needs_policy", item)
            if group == "C" and item.get("application_policy") == "UNRESOLVED":
                count("left_blank_no_information", item)
            if group != "A":
                continue

            # Group A is the answer key: complete, validated, human-entered K/L/M.
            answer_size, answer_uom, answer_pack = answer_key(item)
            blind = BlindInput.from_item(item)

            # The row under test is left out of its own category profile.
            view = profile.view(levels_of(item), leave_out=answer_uom)
            predicted = self.predict_size_and_unit(blind, view)
            if predicted is not None and answer_size is not None:
                size, uom, converted = predicted
                source = f"{_plain(blind.legacy_size)} {blind.legacy_uom}"
                agent_answer = verdict = None
                if uom == ASK_A_PERSON:
                    agrees, kind, agent_answer = False, "SENT_TO_REVIEW", "A person decides"
                    note = (
                        f"{source} could be a weight or fluid ounces in this category, so the "
                        "agent asks a person instead of answering."
                    )
                    # Asking was right only if the plain weight conversion would have been wrong.
                    as_weight = answer_uom == "GM" and within_conversion_tolerance(size, answer_size)
                    verdict = "AGENT_MISS" if as_weight else "CORRECT_CATCH"
                else:
                    agrees, kind, note = compare_size(
                        size, uom, converted, answer_size, answer_uom,
                        given=source, unit_source=f"The legacy unit {blind.legacy_uom}",
                    )
                    if (
                        not agrees and kind == "SIZE" and answer_pack and answer_pack > 1
                        and within_conversion_tolerance(size, answer_size * answer_pack)
                    ):
                        # D1: unit size is one piece. Here the legacy value is the whole pack.
                        kind = "PACK_TOTAL"
                        note = (
                            f"{source} is the whole pack. Your team entered {_plain(answer_size)} "
                            f"{answer_uom} × {_plain(answer_pack)}, which is the same total."
                        )
                    elif not agrees:
                        # The description is an independent witness between legacy and Excel.
                        backs_agent = text_states(item, size, uom, answer_pack)
                        backs_excel = text_states(item, answer_size, answer_uom, answer_pack)
                        if backs_agent and not backs_excel:
                            verdict = "DATA_PROBLEM"
                            note += " The product's own description agrees with the agent."
                        elif backs_excel and not backs_agent:
                            # Production compares every conversion with the description and
                            # stops when they disagree, so this value would not be written.
                            verdict, kind, agent_answer = "CORRECT_CATCH", "SENT_TO_REVIEW", "A person decides"
                            note = (
                                f"{source} does not fit the product's description, so the agent "
                                "asks a person instead of writing it."
                            )
                unit.record(
                    item, agrees, kind, source,
                    agent_answer or f"{_plain(size)} {uom}",
                    f"{_plain(answer_size)} {answer_uom}", note, verdict,
                )

            predicted_pack = self.predict_pack_size(blind)
            if predicted_pack is not None and answer_pack is not None:
                pack_size, fragment = predicted_pack
                agrees = pack_size == answer_pack
                pack.record(
                    item, agrees, "PACK", f"“{fragment}”",
                    _plain(pack_size), _plain(answer_pack),
                    "Same pack size." if agrees else
                    f"The description says “{fragment}”, but Excel has pack size {_plain(answer_pack)}.",
                )

            if is_ai_reading_eligible(item):
                ai_reading_eligible += 1
                first_item.setdefault("ai_reading_eligible", item)

        # A completed on-request AI test is stored on the job; decisions recorded
        # later are applied to it here, exactly as for the other capabilities.
        ai_run = job.get("ai_reading_test") or {}
        ai = (
            CapabilityScore.from_state(ai_run["score"])
            if ai_run.get("status") == "COMPLETED" and ai_run.get("score") else None
        )
        for capability in filter(None, (unit, pack, ai)):
            for kind, total in capability.disagreements.items():
                counters[f"benchmark:{capability.key}:{kind}"] = total
                example = next(
                    (row for row in capability.examples if not row["agrees"] and row["kind"] == kind),
                    None,
                )
                if example:
                    first_item.setdefault(f"benchmark:{capability.key}:{kind}", example)
        # Once the test has run there is nothing left to approve.
        counters["ai_reading_eligible"] = 0 if ai else ai_reading_eligible

        if ai:
            ai_row = ai.as_dict(resolutions) | {
                "available_to_test": ai_reading_eligible,
                "run": {
                    key: ai_run.get(key)
                    for key in ("finished_at", "model_id", "prompt_version", "agent_version", "errors")
                },
            }
        else:
            ai_row = CapabilityScore(AI_READING_KEY, AI_READING_CAPABILITY, (
                "Not measured yet. The AI is shown only the description text of products "
                "whose size is already known, then compared with that size."
            )).as_dict(resolutions) | {"available_to_test": ai_reading_eligible, "run": None}
        capabilities = [unit.as_dict(resolutions), pack.as_dict(resolutions), ai_row]
        measured = [row for row in capabilities if row["tested"]]
        totals: Counter[str] = Counter()
        for row in measured:
            for part in row["breakdown"]:
                totals[part["verdict"]] += part["products"]
        correct = sum(row["correct"] for row in measured)
        scored = sum(row["scored"] for row in measured)
        checks = sum(row["tested"] for row in measured)
        matched = sum(row["agreed"] for row in measured)
        accuracy = {
            # One figure for the whole agent: every check, across every capability.
            "checks": checks,
            "scored": scored,
            "correct": correct,
            "percent": round(correct * 100 / scored, 1) if scored else None,
            "misses": totals["AGENT_MISS"],
            "data_problems": totals["DATA_PROBLEM"],
            "awaiting_decision": totals["NEEDS_DECISION"],
            "bad_values_stopped": totals["CORRECT_CATCH"],
            # Supporting figure only: Excel itself contains mistakes, so this understates the agent.
            "match_percent": round(matched * 100 / checks, 1) if checks else None,
            "breakdown": [
                {"verdict": name, "what_happened": label, "counts_as": counts_as, "products": totals[name]}
                for name, label, counts_as in VERDICTS if totals[name]
            ],
            "capabilities_measured": len(measured),
            "capabilities_total": len(capabilities),
        }
        return {
            "engine": self._engine(job, profile, ai_run),
            "safety_checks": self._safety_checks(items),
            "ai_history": list(job.get("ai_reading_history") or []),
            "result_accuracy": self._result_accuracy(items),
            "version": QUALITY_REPORT_VERSION,
            "decisions_version": decisions_version,
            "dataset": {
                "file_name": job.get("original_file_name"),
                "departments": job.get("selected_departments") or [],
                "products": len(items),
            },
            "accuracy": accuracy,
            "workload": self._workload(items),
            "capabilities": capabilities,
            "decisions": [
                {
                    **decision.model_dump(exclude={"counts_from"}),
                    "products_affected": counters[decision.counts_from],
                    "example": self._decision_example(decision, first_item, unit, pack),
                }
                for decision in decisions
            ],
        }

    @staticmethod
    def _engine(job: dict[str, Any], profile: CategoryProfile, ai_run: dict[str, Any]) -> dict[str, Any]:
        """Which version of the agent produced these figures."""
        policy = job.get("validation_policy") or {}
        return {
            "agent_version": AGENT_VERSION,
            "prompt_version": PROMPT_VERSION,
            "ruleset_version": job.get("ruleset_version"),
            "guards_version": policy.get("guards_version"),
            "processed_with_guards": bool(policy.get("guards_version")),
            "ai_test_prompt_version": ai_run.get("prompt_version"),
            "ai_test_is_current": ai_run.get("prompt_version") in {None, PROMPT_VERSION},
            "liquid_categories": profile.as_dict()["liquid_categories"],
            "mixed_categories": profile.as_dict()["mixed_categories"],
        }

    @staticmethod
    def _safety_checks(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """What the guards did in this workbook, in plain language."""
        found: dict[str, dict[str, Any]] = {}
        for item in items:
            for guard in item.get("guards") or []:
                meta = SAFETY_CHECKS.get(guard.get("code"))
                if not meta:
                    continue
                row = found.setdefault(guard["code"], {
                    "key": guard["code"], "check": meta[0], "effect": meta[1], "products": 0,
                    "example": f"{item.get('item_no')} ({_label(item)}): {guard.get('message')}",
                })
                row["products"] += 1
        return [found[code] for code in SAFETY_CHECKS if code in found]

    @staticmethod
    def _result_accuracy(items: list[dict[str, Any]]) -> dict[str, Any]:
        """Accuracy of what the agent produced: its actions and its suggestions.

        Two witnesses, reported side by side and never mixed into one count:
        independent product text (automatic) and reviewer verdicts (ground truth)."""
        rows = {
            key: {
                "key": key, "type": kind_type, "result": label, "text_checkable": checkable,
                "results": 0, "text_checked": 0, "text_confirmed": 0, "text_contradicted": 0,
                "verified": 0, "verified_correct": 0, "examples": [],
            }
            for key, kind_type, label, checkable in RESULT_KINDS
        }
        for item in items:
            kinds = result_kinds(item)
            if not kinds:
                continue
            proposals = item.get("field_proposals") or {}
            original = item.get("original") or {}
            size = _decimal(proposals.get("standard_size") or original.get("standard_size"))
            uom = str(proposals.get("standard_uom") or original.get("standard_uom") or "").upper()
            pack = _decimal(proposals.get("standard_pack_size") or original.get("standard_pack_size"))
            signals = None
            for kind in kinds:
                row = rows[kind]
                row["results"] += 1
                verdict = reviewer_verdict(item, kind)
                if verdict is not None:
                    row["verified"] += 1
                    row["verified_correct"] += verdict
                if not row["text_checkable"] or size is None:
                    continue
                if signals is None:
                    blind = BlindInput.from_item(item)
                    signals = [
                        extract_field_signals(field, getattr(blind, attribute))
                        for field, attribute in DESCRIPTION_FIELDS
                    ]
                outcome, fragments = text_check(signals, size, uom, pack)
                if outcome == "NO_TEXT":
                    continue
                confirmed = outcome == "CONFIRMED"
                row["text_checked"] += 1
                row["text_confirmed" if confirmed else "text_contradicted"] += 1
                kept = sum(1 for example in row["examples"] if example["confirmed"] == confirmed)
                if kept < (5 if confirmed else MAX_EXAMPLES):
                    stated = ", ".join(f"“{fragment}”" for fragment in dict.fromkeys(fragments))
                    before = (
                        f"{_plain(original.get('standard_size'))} {original.get('standard_uom') or ''}".strip()
                        or "blank"
                    )
                    row["examples"].append({
                        "item_no": item.get("item_no"), "row_number": item.get("row_number"),
                        "product": _label(item), "before": before,
                        "legacy": f"{_plain(original.get('legacy_size'))} {original.get('legacy_uom') or ''}".strip(),
                        "result": f"{_plain(size)} {uom}", "text": stated, "confirmed": confirmed,
                        "note": (
                            f"The description states {stated}, which confirms {_plain(size)} {uom}."
                            if confirmed else
                            f"The description states {stated}, which does not fit {_plain(size)} {uom}."
                        ),
                    })

        def finish(row: dict[str, Any]) -> dict[str, Any]:
            text = (
                round(row["text_confirmed"] * 100 / row["text_checked"], 1)
                if row["text_checked"] else None
            )
            verified = (
                round(row["verified_correct"] * 100 / row["verified"], 1)
                if row["verified"] else None
            )
            use_reviewer = row["verified"] >= MIN_VERIFIED_FOR_HEADLINE
            row.update(
                text_accuracy_percent=text,
                verified_accuracy_percent=verified,
                accuracy_percent=verified if use_reviewer else text,
                basis="REVIEWER" if use_reviewer else ("PRODUCT_TEXT" if text is not None else None),
            )
            row["examples"].sort(key=lambda example: example["confirmed"])
            return row

        finished = [finish(row) for row in rows.values() if row["results"]]
        totals = {
            name: sum(row[name] for row in finished)
            for name in ("results", "text_checked", "text_confirmed", "text_contradicted",
                         "verified", "verified_correct")
        }
        use_reviewer = totals["verified"] >= MIN_VERIFIED_FOR_HEADLINE
        checked = totals["verified"] if use_reviewer else totals["text_checked"]
        correct = totals["verified_correct"] if use_reviewer else totals["text_confirmed"]
        return {
            "rows": finished,
            "headline": {
                **totals,
                "basis": "REVIEWER" if use_reviewer else ("PRODUCT_TEXT" if checked else None),
                "checked": checked,
                "correct": correct,
                "percent": round(correct * 100 / checked, 1) if checked else None,
                "min_verified_for_headline": MIN_VERIFIED_FOR_HEADLINE,
            },
        }

    @staticmethod
    def verification_sample(
        job_id: str, items: Iterable[dict[str, Any]], kind: str, size: int,
    ) -> dict[str, Any]:
        """A repeatable random sample of one kind of result for a reviewer to mark.

        The order is fixed per workbook and kind (hash of job, kind and row), so the
        sample cannot be cherry-picked and verdicts accumulate on the same products.
        A random sample is what lets a small number of checks speak for all results."""
        questions = {
            "FLAGGED": "Was this a real problem that needed a person?",
            "LEFT_BLANK": "Was leaving this product blank the right call?",
        }
        matching = [item for item in items if kind in result_kinds(item)]
        matching.sort(key=lambda item: sha256(
            f"{job_id}:{kind}:{item.get('row_number')}".encode()
        ).hexdigest())
        rows = []
        for item in matching[:size]:
            proposals = item.get("field_proposals") or {}
            original = item.get("original") or {}
            context = item.get("context") or {}

            def value(source: dict[str, Any]) -> str:
                text = f"{_plain(source.get('standard_size'))} {source.get('standard_uom') or ''}".strip()
                pack = source.get("standard_pack_size")
                return (f"{text} × {_plain(pack)}" if pack is not None else text) or "blank"

            final = {key: proposals.get(key) if proposals.get(key) is not None else original.get(key)
                     for key in ("standard_size", "standard_uom", "standard_pack_size")}
            verdict = reviewer_verdict(item, kind)
            rows.append({
                "row_number": item.get("row_number"), "item_no": item.get("item_no"),
                "descriptions": [text for text in (
                    context.get("item_desc_eng"), context.get("item_desc_local_lang"),
                    context.get("web_description_eng"), context.get("web_description_chi"),
                ) if text],
                "legacy": f"{_plain(original.get('legacy_size'))} {original.get('legacy_uom') or ''}".strip() or "—",
                "before": value(original),
                "result": "Left blank" if kind == "LEFT_BLANK" else value(final),
                "reason": "; ".join(
                    str(finding.get("human_reason")) for finding in item.get("findings") or []
                    if finding.get("human_reason")
                ) or None,
                "verdict": None if verdict is None else ("CORRECT" if verdict else "WRONG"),
            })
        return {
            "kind": kind,
            "question": questions.get(kind, "Is the agent’s result right for this product?"),
            "total_results": len(matching),
            "rows": rows,
        }

    @staticmethod
    def _workload(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: Counter[str] = Counter()
        for item in items:
            if item.get("group") == "SKIPPED_PURGED":
                buckets["SKIPPED"] += 1
                continue
            review = (item.get("review") or {}).get("overall_status")
            # The same rule the ledger and the workbook use, so the three never disagree.
            policy = effective_status(item)
            if policy == "REVIEW_REQUIRED" and review in {"APPROVED", "REJECTED", "OVERRIDDEN"}:
                buckets["REVIEWED"] += 1
            elif policy in {"NO_CHANGE", "OBSERVATION_ONLY"}:
                buckets["ALREADY_CORRECT"] += 1
            else:
                buckets[policy] += 1
        live = sum(count for key, count in buckets.items() if key != "SKIPPED")
        rows = (
            ("ALREADY_CORRECT", "Already correct — checked and left untouched"),
            ("AUTO_APPLY", "Corrected automatically"),
            ("REVIEW_REQUIRED", "Needs a person to decide — Excel was not changed"),
            ("REVIEWED", "Decided by a reviewer"),
            ("UNRESOLVED", "Left blank — the workbook has no information to work from"),
            ("SKIPPED", "Skipped — purged products"),
        )
        return [
            {
                "key": key,
                "outcome": label,
                "products": buckets[key],
                "share_percent": (
                    round(buckets[key] * 100 / live, 1) if live and key != "SKIPPED" else None
                ),
            }
            for key, label in rows
            if buckets[key] or key not in {"REVIEWED"}
        ]

    @staticmethod
    def _decision_example(
        decision: BusinessDecision,
        first_item: dict[str, dict[str, Any]],
        *capabilities: "CapabilityScore",
    ) -> str | None:
        source = decision.counts_from
        if source.startswith("benchmark:"):
            example = first_item.get(source)
            return f"{example['item_no']}: {example['note']}" if example else None
        item = first_item.get(source)
        if item is None:
            return None
        item_no, label = item.get("item_no"), _label(item)
        if source.startswith("finding:"):
            code = source.split(":", 1)[1]
            reason = next(
                finding.get("human_reason") for finding in item["findings"]
                if finding.get("code") == code
            )
            return f"{item_no} ({label}): {reason}"
        if source == "reason:NO_RULE":
            original = item.get("original") or {}
            return (
                f"{item_no} ({label}): the legacy size is "
                f"{_plain(original.get('legacy_size'))} {original.get('legacy_uom')}, "
                f"and {original.get('legacy_uom')} has no agreed conversion."
            )
        if source == "left_blank_no_information":
            return f"{item_no} ({label}): no size is stated in the legacy data or in any description."
        return f"{item_no}: {label}"


# Every tested product gets exactly one verdict, so the figures always add up.
#   CORRECT          same answer as the team
#   CORRECT_CATCH    the agent stopped and asked a person, and its reading really did
#                    conflict with Excel: a bad value was kept out of the workbook
#   RULE_APPLIED     the agent followed the agreed definition (D1) and Excel records the
#                    same product differently (a bundle as 1 EA x 2, a case as its total)
#   DATA_PROBLEM     the product's own text states the agent's value and not Excel's.
#                    Left out of the score entirely: nobody has checked the pack.
#   NEEDS_DECISION   the two differ and nothing in the data says which is right.
#                    Left out of the score until the business decides.
#   AGENT_MISS       the agent was wrong, or asked a person when it did not need to
VERDICTS: tuple[tuple[str, str, str], ...] = (
    ("CORRECT", "Same answer as your team", "Correct"),
    ("CORRECT_CATCH", "Stopped a questionable value and asked a person", "Correct"),
    ("RULE_APPLIED", "Followed the agreed rule; Excel records it differently", "Correct"),
    ("AGENT_MISS", "Agent was wrong, or asked a person when it did not need to", "Miss"),
    ("DATA_PROBLEM", "The product's own description disagrees with Excel", "Not scored — problem found in your data"),
    ("NEEDS_DECISION", "Differs, and only your team can say which is right", "Not scored — awaiting your decision"),
)
_COUNTS_AS_CORRECT = frozenset({"CORRECT", "CORRECT_CATCH", "RULE_APPLIED"})


class CapabilityScore:
    def __init__(self, key: str, capability: str, how_tested: str):
        self.key = key
        self.capability = capability
        self.how_tested = how_tested
        self.tested = 0
        self.agreed = 0
        self.no_answer = 0
        self.verdicts: Counter[str] = Counter()
        self.disagreements: Counter[str] = Counter()  # NEEDS_DECISION only, by kind
        self.examples: list[dict[str, Any]] = []

    def record(
        self, item: dict[str, Any], agrees: bool, kind: str,
        source: str, agent: str, excel: str, note: str, verdict: str | None = None,
    ) -> None:
        if verdict is None:
            verdict = "CORRECT" if agrees else (
                "AGENT_MISS" if kind in {"NO_ANSWER", "SENT_TO_REVIEW"} else "NEEDS_DECISION"
            )
        self.tested += 1
        self.verdicts[verdict] += 1
        if agrees:
            self.agreed += 1
        elif kind in {"NO_ANSWER", "SENT_TO_REVIEW"}:
            # The agent did not commit to a value.
            self.no_answer += 1
        if verdict == "NEEDS_DECISION":
            self.disagreements[kind] += 1
        # Keep every kind of outcome visible, plus a few plain matches for context.
        kept = sum(
            1 for example in self.examples
            if example["verdict"] == verdict and example["kind"] == kind
        )
        if kept < (5 if verdict == "CORRECT" else MAX_EXAMPLES):
            self.examples.append({
                "item_no": item.get("item_no"), "row_number": item.get("row_number"),
                "product": _label(item), "source": source, "agent": agent, "excel": excel,
                "agrees": agrees, "kind": kind, "verdict": verdict, "note": note,
            })

    def state(self) -> dict[str, Any]:
        return {
            "key": self.key, "capability": self.capability, "how_tested": self.how_tested,
            "tested": self.tested, "agreed": self.agreed, "no_answer": self.no_answer,
            "verdicts": dict(self.verdicts),
            "disagreements": dict(self.disagreements), "examples": self.examples,
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "CapabilityScore":
        score = cls(state["key"], state["capability"], state["how_tested"])
        score.tested, score.agreed = state["tested"], state["agreed"]
        score.no_answer = state.get("no_answer", 0)
        score.disagreements = Counter(state.get("disagreements") or {})
        # A score stored before verdicts existed: matches are correct, the rest undecided.
        score.verdicts = Counter(state.get("verdicts") or {
            "CORRECT": score.agreed, "AGENT_MISS": score.no_answer,
            "NEEDS_DECISION": sum(score.disagreements.values()),
        })
        score.examples = [
            {"verdict": "CORRECT" if example.get("agrees") else "NEEDS_DECISION", **example}
            for example in state.get("examples") or []
        ]
        return score

    def as_dict(self, resolutions: dict[str, str]) -> dict[str, Any]:
        verdicts = Counter(self.verdicts)
        decided_for = decided_against = 0
        for kind, count in self.disagreements.items():
            resolution = resolutions.get(f"benchmark:{self.key}:{kind}")
            if resolution == "AGENT_CORRECT":
                decided_for += count
            elif resolution == "EXCEL_CORRECT":
                decided_against += count
        # A recorded business decision moves those products out of "awaiting".
        verdicts["NEEDS_DECISION"] -= decided_for + decided_against
        verdicts["CORRECT"] += decided_for
        verdicts["AGENT_MISS"] += decided_against
        correct = sum(verdicts[name] for name in _COUNTS_AS_CORRECT)
        scored = correct + verdicts["AGENT_MISS"]
        awaiting = verdicts["NEEDS_DECISION"]
        return {
            "key": self.key,
            "capability": self.capability,
            "how_tested": self.how_tested,
            "state": "NOT_MEASURED" if not self.tested else (
                "AWAITING_DECISION" if awaiting else "MEASURED"
            ),
            "tested": self.tested,
            "agreed": self.agreed,
            "match_percent": round(self.agreed * 100 / self.tested, 1) if self.tested else None,
            # Agent accuracy: right answer or right action, over everything that can be
            # judged. Data problems and open decisions are in neither side of the fraction.
            "scored": scored,
            "correct": correct,
            "misses": verdicts["AGENT_MISS"],
            "accuracy_percent": round(correct * 100 / scored, 1) if scored else None,
            "data_problems": verdicts["DATA_PROBLEM"],
            "awaiting_decision": awaiting,
            "no_answer": self.no_answer,
            "confirmed_correct": decided_for,
            "confirmed_incorrect": decided_against,
            "breakdown": [
                {"verdict": name, "what_happened": label, "counts_as": counts_as, "products": verdicts[name]}
                for name, label, counts_as in VERDICTS if verdicts[name]
            ],
            "available_to_test": None,
            "examples": sorted(
                self.examples,
                key=lambda example: [name for name, *_ in VERDICTS].index(example["verdict"]) if example["verdict"] != "CORRECT" else 99,
            ),
        }
