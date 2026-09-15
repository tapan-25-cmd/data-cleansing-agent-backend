from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class InputProduct(BaseModel):
    model_config = ConfigDict(frozen=True)

    row_number: int
    item_no: str
    division: str | None = None
    department: str | None = None
    category: str | None = None
    subcategory: str | None = None
    section: str | None = None
    legacy_size: Decimal | str | None = None
    legacy_uom: str | None = None
    standard_size: Decimal | str | None = None
    standard_uom: str | None = None
    standard_pack_size: Decimal | str | None = None
    web_description_eng: str | None = None
    web_description_chi: str | None = None
    item_brand_eng: str | None = None
    item_brand_local: str | None = None
    item_desc_eng: str | None = None
    item_desc_local: str | None = None


class RuleProposal(BaseModel):
    standard_size: Decimal
    standard_uom: str
    rule_id: str
    source_uom: str
    target_uom: str
    factor: Decimal
    source_value: Decimal
    raw_target: Decimal
    final_target: Decimal
