from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import re

from app.domain.product import InputProduct


PACKAGING_EXPRESSION_VERSION = "packaging-expression-v2"

# PRD-approved independent description fields. Excel columns B/C
# (product_description/product_description_local) are deliberately impossible
# to access here because they are not part of InputProduct.
PACKAGING_EVIDENCE_FIELDS = (
    ("item_desc_eng", "item_desc_eng"),
    ("item_desc_local_lang", "item_desc_local"),
    ("web_description_eng", "web_description_eng"),
    ("web_description_chi", "web_description_chi"),
)

_UNIT_FACTORS = {
    "G": ("GM", Decimal("1")),
    "GM": ("GM", Decimal("1")),
    "KG": ("GM", Decimal("1000")),
    "ML": ("ML", Decimal("1")),
    "L": ("ML", Decimal("1000")),
    "LT": ("ML", Decimal("1000")),
    "OZ": ("GM", Decimal("28.349523125")),
    "LB": ("GM", Decimal("453.59237")),
}

_CASE_EXPRESSION = re.compile(
    r"(?:(?P<outer>\d+)\s*)?\b(?:CASE|CS)\b\s*/\s*"
    r"(?P<inner>\d+)\s*(?:X|×)\s*"
    r"(?P<size>\d+(?:\.\d+)?)\s*(?P<uom>KG|GM|G|ML|LT|L|OZ|LB)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PackagingExpression:
    field: str
    fragment: str
    outer_count: int | None
    inner_count: int
    measurement: Decimal
    standard_uom: str

    def candidates(self) -> tuple[dict[str, object], ...]:
        candidates: list[dict[str, object]] = [{
            "interpretation": "PER_UNIT",
            "standard_size": str(self.measurement),
            "standard_uom": self.standard_uom,
            "standard_pack_size": str(self.inner_count),
        }]
        if self.outer_count is not None:
            candidates.append({
                "interpretation": "OUTER_TOTAL",
                "standard_size": str(self.measurement * self.outer_count),
                "standard_uom": self.standard_uom,
                "standard_pack_size": str(self.inner_count),
            })
            candidates.append({
                "interpretation": "NESTED_COUNT",
                "standard_size": str(self.measurement),
                "standard_uom": self.standard_uom,
                "standard_pack_size": str(self.outer_count * self.inner_count),
            })
        return tuple(candidates)

    def as_dict(self) -> dict[str, object]:
        return {
            "version": PACKAGING_EXPRESSION_VERSION,
            "field": self.field,
            "fragment": self.fragment,
            "outer_count": self.outer_count,
            "inner_count": self.inner_count,
            "measurement": str(self.measurement),
            "standard_uom": self.standard_uom,
            "candidates": list(self.candidates()),
        }


def extract_packaging_expressions(product: InputProduct) -> tuple[PackagingExpression, ...]:
    results: list[PackagingExpression] = []
    seen: set[tuple[int | None, int, Decimal, str]] = set()
    for evidence_field, product_attribute in PACKAGING_EVIDENCE_FIELDS:
        text = getattr(product, product_attribute)
        if not text:
            continue
        for match in _CASE_EXPRESSION.finditer(text):
            raw_uom = match.group("uom").upper()
            standard_uom, factor = _UNIT_FACTORS[raw_uom]
            expression = PackagingExpression(
                field=evidence_field,
                fragment=match.group(0),
                outer_count=int(match.group("outer")) if match.group("outer") else None,
                inner_count=int(match.group("inner")),
                measurement=Decimal(match.group("size")) * factor,
                standard_uom=standard_uom,
            )
            identity = (
                expression.outer_count,
                expression.inner_count,
                expression.measurement,
                expression.standard_uom,
            )
            if identity not in seen:
                seen.add(identity)
                results.append(expression)
    return tuple(results)


def matching_interpretations(
    expression: PackagingExpression,
    *,
    standard_size: Decimal,
    standard_uom: str,
    standard_pack_size: Decimal,
) -> tuple[str, ...]:
    matches: list[str] = []
    for candidate in expression.candidates():
        if (
            Decimal(str(candidate["standard_size"])) == standard_size
            and candidate["standard_uom"] == standard_uom
            and Decimal(str(candidate["standard_pack_size"])) == standard_pack_size
        ):
            matches.append(str(candidate["interpretation"]))
    return tuple(matches)
