import re
from dataclasses import dataclass
from decimal import Decimal

from app.domain.product import InputProduct


SIGNAL = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*(KG|GM|G|ML|LT|L|OZ|LB)(?![A-Z])", re.IGNORECASE)


@dataclass(frozen=True)
class SizeSignal:
    value: Decimal
    dimension: str
    field: str
    fragment: str


def extract_signal(field: str, text: str | None) -> SizeSignal | None:
    if not text:
        return None
    match = SIGNAL.search(text)
    if not match:
        return None
    value = Decimal(match.group(1))
    unit = match.group(2).upper()
    factors = {
        "KG": ("WEIGHT", Decimal("1000")), "GM": ("WEIGHT", Decimal("1")),
        "G": ("WEIGHT", Decimal("1")), "OZ": ("WEIGHT", Decimal("28.349523125")),
        "LB": ("WEIGHT", Decimal("453.59237")), "LT": ("VOLUME", Decimal("1000")),
        "L": ("VOLUME", Decimal("1000")), "ML": ("VOLUME", Decimal("1")),
    }
    dimension, factor = factors[unit]
    return SizeSignal(value * factor, dimension, field, match.group(0))


def find_discrepancies(product: InputProduct) -> list[dict[str, object]]:
    pairs = (
        ("brand", "item_brand_eng", product.item_brand_eng, "item_brand_local_lang", product.item_brand_local),
        ("item_desc", "item_desc_eng", product.item_desc_eng, "item_desc_local_lang", product.item_desc_local),
        ("web_desc", "web_description_eng", product.web_description_eng, "web_description_chi", product.web_description_chi),
    )
    details: list[dict[str, object]] = []
    for pair, left_field, left_text, right_field, right_text in pairs:
        left = extract_signal(left_field, left_text)
        right = extract_signal(right_field, right_text)
        if left and right and (left.dimension != right.dimension or left.value != right.value):
            details.append({
                "pair": pair,
                "left": {"field": left.field, "value": str(left.value), "fragment": left.fragment},
                "right": {"field": right.field, "value": str(right.value), "fragment": right.fragment},
            })
    return details
