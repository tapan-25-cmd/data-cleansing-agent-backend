from typing import Any, Mapping

from app.services.normalization import blank


PURGE_FIELDS = (
    "item_brand_eng",
    "item_brand_local_lang",
    "item_desc_eng",
    "item_desc_local_lang",
    "business_unit_no",
    "business_unit_name",
    "department_no",
    "department_name",
    "category_no",
    "category_name",
    "sub_category_no",
    "sub_category_name",
    "section_no",
    "section_name",
)


def is_purged(row: Mapping[str, Any]) -> bool:
    return all(blank(row.get(field)) for field in PURGE_FIELDS)
