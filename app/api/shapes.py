"""Every shape a row can have (which of I, J, K, L, M are filled) and where it goes."""
from itertools import product as combinations

from fastapi import APIRouter

from app.domain.enums import WorkGroup
from app.domain.product import InputProduct
from app.services.classifier import classify

router = APIRouter(prefix="/rules", tags=["rules"])

FIELDS = [
    {"key": "I", "name": "Old size", "column": "I", "example": "350", "story": "The number from the old system."},
    {"key": "J", "name": "Old unit", "column": "J", "example": "GM", "story": "The unit that goes with it: grams, millilitres, pieces."},
    {"key": "K", "name": "Size", "column": "K", "example": "70", "story": "How much is in one piece."},
    {"key": "L", "name": "Unit", "column": "L", "example": "GM", "story": "Grams, millilitres or pieces. Always one of these three."},
    {"key": "M", "name": "Pack", "column": "M", "example": "5", "story": "How many pieces are sold together."},
]

GROUPS = {
    "A": {"name": "Group A · Already filled in",
          "story": "Size, unit and pack are all filled in. The tool does not change them. It checks them against the old size and the product description, and asks a person when something does not match."},
    "B": {"name": "Group B · Filled in from the old size",
          "story": "Size and unit are empty, but the old system has a size and unit. The tool converts it with a fixed table, for example 1 KG becomes 1000 GM, and fills it in."},
    "C": {"name": "Group C · Read from the description",
          "story": "There is no old size to use. The tool reads the product description and fills in a size only if one is written there. If nothing is written, it leaves the row empty."},
    "PURGED": {"name": "Purged · Skipped",
               "story": "The product is marked as purged in the workbook and has no details. It is checked first, before any other rule, and skipped. Nothing is read or changed, and it comes back in the download exactly as it was."},
    "INVALID": {"name": "Incomplete row",
                "story": "Only part of size, unit and pack is filled in. The tool does not guess the rest. It marks the row so someone can complete it."},
}


def _outcome(group: WorkGroup, has: dict[str, bool]) -> tuple[str, str]:
    if group == WorkGroup.A:
        if has["I"] and has["J"]:
            return "Checked", "K, L and M are compared with the old size and with the product's words. Kept if they agree, sent to a person if they do not."
        return "Checked, old size unusable", "K, L and M are checked against the product's words only; the old size is incomplete and cannot be used as a witness."
    if group == WorkGroup.B:
        if has["I"]:
            return "Filled in", "The old size is converted with the unit table and written into K and L." + (" M is kept as it is." if has["M"] else " With no pack count written anywhere, the pack size becomes 1.")
        return "Left empty", "There is a unit in J but no size in I, so there is nothing to convert. The row stays empty with a note."
    if group == WorkGroup.C:
        if has["I"]:
            return "Read from the words", "The old size has no unit, so it cannot be trusted. The description is read; a size is written only if it is literally there."
        return "Read from the words", "The description is read; a size is written only if it is literally there, otherwise the row stays empty."
    return "Reported as incomplete", "Some of K, L, M are filled but not all three. Nothing is checked or written; the row is marked invalid so someone can complete or clear it."


@router.get("/shapes")
def shapes() -> dict:
    rows = []
    for bits in combinations([True, False], repeat=5):
        has = dict(zip(("I", "J", "K", "L", "M"), bits))
        product = InputProduct(
            row_number=1, item_no="000001", department="03_Grocery 2",
            legacy_size="350" if has["I"] else None, legacy_uom="GM" if has["J"] else None,
            standard_size="70" if has["K"] else None, standard_uom="GM" if has["L"] else None,
            standard_pack_size="5" if has["M"] else None,
        )
        group = classify(product)
        key = {WorkGroup.A: "A", WorkGroup.B: "B", WorkGroup.C: "C"}.get(group, "INVALID")
        outcome, detail = _outcome(group, has)
        rows.append({"has": has, "group": key, "outcome": outcome, "detail": detail})
    return {"fields": FIELDS, "groups": GROUPS, "rows": rows}
