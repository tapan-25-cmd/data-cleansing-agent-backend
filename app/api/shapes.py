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
    "A": {"name": "Group A · Already filled in", "story": "The three boxes K, L and M are all filled. The tool does not write anything here. It only checks whether the numbers make sense against the old size and the product's own words, and raises a hand when they do not.",
          "kid": "Like homework that is already done: the teacher only marks it."},
    "B": {"name": "Group B · Fill in from the old size", "story": "K and L are empty but the old size and unit exist. The tool converts the old size into the standard unit with a fixed table, for example 1 KG becomes 1000 GM, and writes it in.",
          "kid": "Like copying an answer from your old notebook, but in the new language."},
    "C": {"name": "Group C · Read the words", "story": "There is no old size to convert and K and L are empty. The only clue is the product's description, so the tool reads it and writes down a size only if one is literally written there.",
          "kid": "Like finding the answer written on the box, and writing nothing if the box says nothing."},
    "INVALID": {"name": "Invalid row · Half filled", "story": "Only one or two of K, L and M are filled. The row is neither complete enough to check nor empty enough to fill in, so it is reported back as it is.",
                "kid": "Like a form with a name but no address: we send it back rather than guess."},
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
