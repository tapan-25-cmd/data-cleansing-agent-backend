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

# The group is the outcome, decided after the row has been worked on.
GROUPS = {
    "A": {"name": "Group A · No change",
          "story": "Nothing in K, L or M was changed. The values were checked and kept, sometimes with a note explaining why."},
    "B": {"name": "Group B · Changed by the tool",
          "story": "The tool wrote a value into K, L or M itself: converted from the old size, read from the description, or completed from what the row already had."},
    "C": {"name": "Group C · Raised for a person",
          "story": "The tool could not settle the row on its own: sources disagree, a value is only suggested, nothing is written anywhere, or a value cannot be used. Nothing is written until a person decides."},
    "PURGED": {"name": "Purged · Skipped",
               "story": "The product is marked as purged in the workbook and has no details. It is checked first, before any other rule, and skipped. Nothing is read or changed, and it comes back in the download exactly as it was."},
}

# The method is chosen from the row's shape before anything is worked out.
ROUTES = {
    "A": {"name": "Check existing values", "story": "Size, unit and pack are all filled in, so they are checked, not rewritten."},
    "B": {"name": "Convert from the old size", "story": "Size and unit are empty and the old system has a unit, so the old size is converted with the unit table."},
    "C": {"name": "Read from the description", "story": "Size and unit are empty and there is no old unit, so the product description is read."},
    "INCOMPLETE": {"name": "Complete a half-filled row", "story": "Some of size, unit and pack are filled. They are kept; the missing ones are looked for in the old size and the description."},
}


def _outcome(route: WorkGroup, has: dict[str, bool]) -> tuple[list[str], str, str]:
    """The groups a row of this shape can end in, a short verdict, and what happens."""
    if route == WorkGroup.A:
        if has["I"] and has["J"]:
            return ["A", "C"], "Checked", "K, L and M are compared with the old size and with the product's words. Kept if they agree (A); raised if they do not (C)."
        return ["A", "C"], "Checked, old size unusable", "K, L and M are checked against the product's words only; the old size is incomplete and cannot be used as a witness. Kept (A) or raised (C)."
    if route == WorkGroup.B:
        if has["I"]:
            return ["B", "C"], "Filled in from the old size", "The old size is converted with the unit table and written into K and L (B)." + (" M is kept as it is." if has["M"] else " With no pack count written anywhere, the pack size becomes 1.") + " An ounce that could be weight or fluid, or a description that disagrees, is raised instead (C)."
        return ["C"], "Could not determine", "There is a unit in J but no size in I, so there is nothing to convert. The row is raised with a note (C)."
    if route == WorkGroup.C:
        if has["I"]:
            return ["B", "C"], "Read from the words", "The old size has no unit, so it cannot be used. The description is read; a size written there is filled in (B), otherwise the row is raised as could not determine (C)."
        return ["B", "C"], "Read from the words", "The description is read; a size written there is filled in (B), otherwise the row is raised as could not determine (C)."
    return ["B", "C"], "Completed where found", "What is filled is kept. Each missing value is looked for in the old size and the description: found and certain, it is written (B); only suggested or written nowhere, the row is raised (C)."


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
        route = classify(product)
        groups, outcome, detail = _outcome(route, has)
        rows.append({"has": has, "route": route.value, "route_name": ROUTES[route.value]["name"],
                     "groups": groups, "outcome": outcome, "detail": detail})
    return {"fields": FIELDS, "groups": GROUPS, "routes": ROUTES, "rows": rows}
