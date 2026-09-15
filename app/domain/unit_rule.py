from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.enums import BASE_UNITS


class UnitRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_id: str
    source_uoms: tuple[str, ...]
    target_uom: str
    operation: Literal["MULTIPLY"] = "MULTIPLY"
    factor: Decimal = Field(gt=0)
    rounding_decimals: int | None = Field(default=None, ge=0)
    enabled: bool = True
    notes: str = ""

    @field_validator("target_uom")
    @classmethod
    def validate_target(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in BASE_UNITS:
            raise ValueError(f"target_uom must be one of {sorted(BASE_UNITS)}")
        return normalized

    @field_validator("source_uoms")
    @classmethod
    def validate_sources(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip().upper() for value in values if value.strip())
        if not normalized:
            raise ValueError("source_uoms cannot be empty")
        if len(set(normalized)) != len(normalized):
            raise ValueError("source_uoms contains duplicates")
        return normalized


class UnitRuleset(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str
    rules: tuple[UnitRule, ...]
