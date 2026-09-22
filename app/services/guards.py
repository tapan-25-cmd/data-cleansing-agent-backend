"""Sanity checks on a proposed result, whatever produced it (rule, text or AI).

A guard never invents or changes a value. It either explains a decision (INFO) or stops
an automatic write and sends the row to a person with a plain-language reason (REVIEW).
Each guard is small, versioned and tested on its own; see docs/agent-intelligence-plan.md.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

from app.services.category_profile import CategoryProfile, CategoryView
from app.services.discrepancy_service import FieldSignals, extract_field_signals, text_check

GUARDS_VERSION = "guards-v1"
FLUID_OUNCE_UOM = "FL OZ"
COUNT_UNITS = frozenset({"PC", "PK", "PACK", "EA"})
# Codes that stop an automatic write.
GUARD_REVIEW_CODES = frozenset({
    "OUNCE_MAY_BE_FLUID",
    "TEXT_CONTRADICTS_RESULT",
    "SIZE_OUTSIDE_CATEGORY_RANGE",
    "AI_MEASUREMENT_NOT_PRODUCT_SIZE",
    "LEGACY_MAY_BE_PACK_TOTAL",
    "AI_PACK_NEEDS_CONFIRMATION",
})


def is_sellable_pack(result: Any) -> bool:
    """D1: only a sellable pack becomes a pack size; contents and outer cases do not.

    A missing role is a reading from before agent contract v3 and is accepted."""
    return getattr(result, "pack_role", None) in {None, "SELLABLE_PACK"}


def derived_confidence(measurement: Any, request: Any, *, blocked: bool) -> str:
    """Confidence from evidence, not from the model's own opinion of itself.

    HIGH when the same reading appears in a second text field (the other language, or
    the other description); MEDIUM for a single clean source; LOW when a guard stopped it."""
    if blocked:
        return "LOW"
    wanted = _squash(measurement.fragment)
    for name in _TEXT_FIELDS:
        text = getattr(request, name, None)
        if name == measurement.field or not text:
            continue
        for signal in extract_field_signals(name, text).measurements:
            same_reading = signal.source_value == measurement.value and signal.source_uom == measurement.uom
            if same_reading or _squash(signal.fragment) == wanted:
                return "HIGH"
    return "MEDIUM"


_TEXT_FIELDS = (
    "item_desc_eng", "item_desc_local_lang", "web_description_eng", "web_description_chi",
)


def _squash(fragment: str) -> str:
    return fragment.replace(" ", "").upper()


_ROLE_WORDS = {
    "CAPACITY_OR_RANGE": "the capacity or measuring range of a container or tool",
    "DIMENSION": "a dimension of the product",
    "NAME_OR_GRADE": "part of the product's name or grade",
    "UNCLEAR": "something the AI could not identify",
}


def ai_pack_needs_confirmation(pack_size: Decimal, fragment: str) -> dict[str, Any]:
    """An AI pack size is only ever asked for where the text was too loose for a rule
    ("12PCS COUPON", "1PC", "4'S"). Whether such a count is the sellable pack is an open
    business decision, so a person confirms it; it is never written automatically.
    Seen live on v0.2: vouchers read as pack 12 and pack 1."""
    return _issue(
        "AI_PACK_NEEDS_CONFIRMATION", "standard_pack_size", "REVIEW",
        f"The AI read a pack size of {plain(pack_size)} from “{fragment}”. A piece count can "
        "mean the pack being sold or what is inside it (a gift box, a voucher), so a person "
        "confirms it before it is used.",
        fragment, plain(pack_size),
    )


def role_meaning(role: str | None) -> str:
    return _ROLE_WORDS.get(role or "", "something other than an amount of product")


def not_a_product_size(measurement: Any) -> dict[str, Any]:
    meaning = _ROLE_WORDS.get(measurement.role, "not an amount of product")
    return _issue(
        "AI_MEASUREMENT_NOT_PRODUCT_SIZE", "standard_size", "REVIEW",
        f"The AI read “{measurement.fragment}” but identified it as {meaning}, so it was "
        "not used as the product size.",
        measurement.fragment,
    )


def _issue(code: str, field: str, severity: str, message: str,
           current: object | None = None, expected: object | None = None) -> dict[str, Any]:
    return {
        "code": code, "field": field, "severity": severity, "message": message,
        "current_value": current, "expected_value": expected, "guard_version": GUARDS_VERSION,
    }


def plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


def ounce_reading(legacy_uom: str | None, view: CategoryView) -> str:
    """How to read a legacy ``OZ``: WEIGHT, VOLUME (fluid ounce) or REVIEW (category is mixed)."""
    if (legacy_uom or "").strip().upper() != "OZ":
        return "WEIGHT"
    if view.liquid:
        return "VOLUME"
    return "REVIEW" if view.mixed else "WEIGHT"


def fluid_ounce_note(legacy_size: object, category: str | None) -> dict[str, Any]:
    return _issue(
        "OUNCE_READ_AS_FLUID", "standard_uom", "INFO",
        f"{legacy_size} OZ was converted as fluid ounces because products in "
        f"{category or 'this category'} are sold by volume.",
    )


def fluid_ounce_review(legacy_size: object, weight: Decimal, volume: Decimal,
                       category: str | None) -> dict[str, Any]:
    return _issue(
        "OUNCE_MAY_BE_FLUID", "standard_uom", "REVIEW",
        f"{legacy_size} OZ could be a weight ({plain(weight)} GM) or fluid ounces "
        f"({plain(volume)} ML). Products in {category or 'this category'} use both, so a "
        "person should choose.",
        f"{plain(weight)} GM", f"{plain(volume)} ML",
    )


def text_contradiction(signals: Iterable[FieldSignals], size: Decimal, uom: str,
                       pack_size: Decimal | None) -> dict[str, Any] | None:
    """Independent product text states a size that does not fit the proposed value."""
    outcome, fragments = text_check(signals, size, uom, pack_size)
    if outcome != "CONTRADICTED":
        return None
    stated = ", ".join(f"“{fragment}”" for fragment in dict.fromkeys(fragments))
    return _issue(
        "TEXT_CONTRADICTS_RESULT", "standard_size", "REVIEW",
        f"The description states {stated}, which does not fit {plain(size)} {uom}.",
        f"{plain(size)} {uom}", stated,
    )


def legacy_is_pack_total(signals: Iterable[FieldSignals], size: Decimal, uom: str,
                         pack_size: Decimal | None) -> dict[str, Any] | None:
    """D1: unit size is one piece. The legacy value may be the whole pack instead.

    Raised only when the text itself states the per-piece size (legacy / pack) and does
    not state the legacy value. On v0.2 that signal was right 3 times out of 3; a
    size-range signal was right only 6 times out of 10 and is deliberately not used."""
    if pack_size is None or pack_size <= 1 or uom not in {"GM", "ML"}:
        return None
    per_piece = size / pack_size
    stated = [m for field in signals for m in field.measurements if m.uom == uom]
    if not any(abs(m.value - per_piece) <= 1 for m in stated):
        return None
    if any(abs(m.value - size) <= 1 for m in stated):
        return None
    fragment = next(m.fragment for m in stated if abs(m.value - per_piece) <= 1)
    return _issue(
        "LEGACY_MAY_BE_PACK_TOTAL", "standard_size", "REVIEW",
        f"The legacy size {plain(size)} {uom} looks like the whole pack: the description "
        f"states “{fragment}” per piece and the pack size is {plain(pack_size)}.",
        f"{plain(size)} {uom}", f"{plain(per_piece)} {uom} × {plain(pack_size)}",
    )


def implausible_size(profile: CategoryProfile, levels: dict[str, str | None],
                     size: Decimal, uom: str, *, read_from_text: bool) -> dict[str, Any] | None:
    """A size far outside what this category normally holds.

    For a size *read from text* this stops the write: it is how a misread number
    (500 KG of pasta, 3.3 G of yoghurt) is caught. For a mechanical conversion of the
    legacy value it is only a note, because the conversion itself is right; measured on
    v0.2 it flagged 7 rows, all genuine large gift packs."""
    if uom not in {"GM", "ML"} or profile.plausible(levels, uom, size) is not False:
        return None
    low, high = profile.size_band(levels, uom)  # type: ignore[misc]
    normal = f"{plain(low)}–{plain(high)} {uom}"
    if read_from_text:
        return _issue(
            "SIZE_OUTSIDE_CATEGORY_RANGE", "standard_size", "REVIEW",
            f"{plain(size)} {uom} is far outside the sizes normally seen in this category "
            f"({normal}), so a person should confirm it.",
            f"{plain(size)} {uom}", normal,
        )
    return _issue(
        "SIZE_UNUSUAL_FOR_CATEGORY", "standard_size", "INFO",
        f"{plain(size)} {uom} is unusually large or small for this category ({normal}). "
        "The conversion itself is exact.",
        f"{plain(size)} {uom}", normal,
    )


def count_in_weight_category(legacy_uom: str | None, proposed_uom: str,
                             view: CategoryView) -> dict[str, Any] | None:
    """D5: PC = EA is applied, with a note where people usually enter a weight or volume."""
    share = view.share("EA")
    if (legacy_uom or "").upper() not in COUNT_UNITS or proposed_uom != "EA":
        return None
    if share is None or share >= Decimal("0.5"):
        return None
    return _issue(
        "COUNT_IN_MEASURED_CATEGORY", "standard_uom", "INFO",
        "The legacy data gives only a count. Most products in this category carry a weight "
        "or volume, which may be printed on the pack.",
    )
