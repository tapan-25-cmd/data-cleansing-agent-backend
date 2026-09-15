from decimal import Decimal, InvalidOperation
from typing import Any


def blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def clean_text(value: Any) -> str | None:
    if blank(value):
        return None
    return str(value).strip()


def clean_uom(value: Any) -> str | None:
    text = clean_text(value)
    return text.upper() if text else None


def decimal_value(value: Any) -> Decimal | None:
    if blank(value):
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
