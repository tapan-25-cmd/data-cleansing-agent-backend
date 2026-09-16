from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from hashlib import sha256
import json
from typing import Any

from app.services.discrepancy_service import extract_signal, find_discrepancies
from app.services.excel_reader import FIELD_MAP, WorkbookRow
from app.services.normalization import blank
from app.services.rule_engine import RuleEngine


GROUP_A_VALIDATION_VERSION = "group-a-validation-v1"
STANDARD_UOM_ALIAS_VERSION = "standard-uom-aliases-v1"


class ValidationSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class GroupAValidationStatus(str, Enum):
    VALID = "VALID"
    AUTO_FIX = "AUTO_FIX"
    REVIEW = "REVIEW"
    INVALID = "INVALID"


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    field: str
    severity: ValidationSeverity
    message: str
    current_value: object | None = None
    expected_value: object | None = None

    def as_dict(self) -> dict[str, object | None]:
        return {
            "code": self.code,
            "field": self.field,
            "severity": self.severity.value,
            "message": self.message,
            "current_value": _json_value(self.current_value),
            "expected_value": _json_value(self.expected_value),
        }


@dataclass(frozen=True)
class GroupAValidationResult:
    status: GroupAValidationStatus
    standard_size: Decimal
    standard_uom: str
    standard_pack_size: Decimal
    issues: tuple[ValidationIssue, ...]

    @property
    def has_warnings(self) -> bool:
        return any(issue.severity == ValidationSeverity.WARNING for issue in self.issues)

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_version": GROUP_A_VALIDATION_VERSION,
            "alias_version": STANDARD_UOM_ALIAS_VERSION,
            "alias_checksum": STANDARD_UOM_ALIAS_CHECKSUM,
            "status": self.status.value,
            "issues": [issue.as_dict() for issue in self.issues],
            "canonical_values": {
                "standard_size": str(self.standard_size),
                "standard_uom": self.standard_uom,
                "standard_pack_size": str(self.standard_pack_size),
            },
        }

    def normalization_proposal(self) -> dict[str, str]:
        """Return exact canonical B3 values without treating cleanup as conversion."""
        return {
            "standard_size": str(self.standard_size),
            "standard_uom": self.standard_uom,
            "standard_pack_size": str(self.standard_pack_size.quantize(Decimal("1"))),
        }


# Aliases here represent the same unit and therefore never change the numeric value.
# Units that require a factor (KG, OZ, LB, L, LT) remain in the conversion ruleset.
STANDARD_UOM_ALIASES: dict[str, str] = {
    "EA": "EA",
    "EACH": "EA",
    "GM": "GM",
    "G": "GM",
    "GRAM": "GM",
    "GRAMS": "GM",
    "GRAMME": "GM",
    "GRAMMES": "GM",
    "ML": "ML",
    "MILLILITER": "ML",
    "MILLILITERS": "ML",
    "MILLILITRE": "ML",
    "MILLILITRES": "ML",
    "FT": "FT",
    "FOOT": "FT",
    "FEET": "FT",
}
STANDARD_UOM_ALIAS_CHECKSUM = sha256(
    json.dumps(STANDARD_UOM_ALIASES, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


DESCRIPTION_FIELDS = (
    ("item_brand_eng", "item_brand_eng"),
    ("item_brand_local_lang", "item_brand_local"),
    ("item_desc_eng", "item_desc_eng"),
    ("item_desc_local_lang", "item_desc_local"),
    ("web_description_eng", "web_description_eng"),
    ("web_description_chi", "web_description_chi"),
)


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _parse_positive_decimal(
    value: object,
    *,
    field: str,
    integral: bool,
) -> tuple[Decimal | None, list[ValidationIssue], bool]:
    issues: list[ValidationIssue] = []
    auto_fix = False
    if isinstance(value, bool) or isinstance(value, (date, datetime, time)):
        return None, [ValidationIssue(
            "INVALID_DATA_TYPE", field, ValidationSeverity.ERROR,
            f"{field} must be a positive number", value,
        )], False
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("="):
            return None, [ValidationIssue(
                "FORMULA_NOT_ALLOWED", field, ValidationSeverity.ERROR,
                f"{field} must contain a fixed value, not a formula", value,
            )], False
        if stripped.startswith("#"):
            return None, [ValidationIssue(
                "EXCEL_ERROR_VALUE", field, ValidationSeverity.ERROR,
                f"{field} contains an Excel error", value,
            )], False
        auto_fix = True
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None, [ValidationIssue(
            "NOT_NUMERIC", field, ValidationSeverity.ERROR,
            f"{field} must be numeric", value,
        )], False
    if not parsed.is_finite():
        return None, [ValidationIssue(
            "NOT_FINITE", field, ValidationSeverity.ERROR,
            f"{field} must be finite", value,
        )], False
    if parsed <= 0:
        return None, [ValidationIssue(
            "NOT_POSITIVE", field, ValidationSeverity.ERROR,
            f"{field} must be greater than zero", value,
        )], False
    if integral and parsed != parsed.to_integral_value():
        return None, [ValidationIssue(
            "NOT_WHOLE_NUMBER", field, ValidationSeverity.ERROR,
            f"{field} must be a whole number", value,
        )], False
    if auto_fix:
        issues.append(ValidationIssue(
            "NUMERIC_TEXT_NORMALIZATION", field, ValidationSeverity.INFO,
            f"{field} will be stored as a number", value, str(parsed),
        ))
    return parsed, issues, auto_fix


class GroupAValidator:
    """Validate complete K/L/M candidates before they are trusted as Group A."""

    def __init__(self, comparison_engine: RuleEngine):
        self.comparison_engine = comparison_engine

    def validate(
        self,
        row: WorkbookRow,
        *,
        duplicate_item_number: bool = False,
    ) -> GroupAValidationResult | None:
        raw_size = row.raw.get(FIELD_MAP["standard_size"])
        raw_uom = row.raw.get(FIELD_MAP["standard_uom"])
        raw_pack = row.raw.get(FIELD_MAP["standard_pack_size"])

        # This validator owns complete fields that are already base units or known
        # same-unit aliases. Convertible/unknown units continue through Group B.
        if blank(raw_size) or blank(raw_uom) or blank(raw_pack):
            return None
        normalized_uom = str(raw_uom).strip().upper()
        canonical_uom = STANDARD_UOM_ALIASES.get(normalized_uom)
        if canonical_uom is None:
            return None

        issues: list[ValidationIssue] = []
        invalid = False
        auto_fix = False

        if not row.product.item_no:
            invalid = True
            issues.append(ValidationIssue(
                "MISSING_ITEM_NUMBER", "item_no", ValidationSeverity.ERROR,
                "Item number is required",
            ))
        if duplicate_item_number:
            invalid = True
            issues.append(ValidationIssue(
                "DUPLICATE_ITEM_NUMBER", "item_no", ValidationSeverity.ERROR,
                "Item number occurs more than once in the selected dataset",
                row.product.item_no,
            ))

        size, size_issues, size_fix = _parse_positive_decimal(
            raw_size, field="standard_size", integral=False
        )
        pack, pack_issues, pack_fix = _parse_positive_decimal(
            raw_pack, field="standard_pack_size", integral=True
        )
        issues.extend(size_issues)
        issues.extend(pack_issues)
        invalid = invalid or size is None or pack is None
        auto_fix = auto_fix or size_fix or pack_fix

        if not isinstance(raw_uom, str):
            invalid = True
            issues.append(ValidationIssue(
                "INVALID_UOM_TYPE", "standard_uom", ValidationSeverity.ERROR,
                "Standardized UOM must be text", raw_uom,
            ))
        elif raw_uom != canonical_uom:
            auto_fix = True
            issues.append(ValidationIssue(
                "UOM_CANONICALIZATION", "standard_uom", ValidationSeverity.INFO,
                "Standardized UOM requires canonical spelling and casing",
                raw_uom, canonical_uom,
            ))

        # Use safe placeholders only so every result remains serializable. INVALID
        # results are never used to create proposals.
        checked_size = size if size is not None else Decimal("0")
        checked_pack = pack if pack is not None else Decimal("0")

        if not invalid:
            issues.extend(self._legacy_issues(row, checked_size, canonical_uom))
            conflicts = find_discrepancies(row.product)
            for conflict in conflicts:
                issues.append(ValidationIssue(
                    "BILINGUAL_DESCRIPTION_CONFLICT",
                    str(conflict.get("pair") or "description"),
                    ValidationSeverity.ERROR,
                    "Paired description fields contain conflicting explicit measurements",
                    conflict,
                ))
            issues.extend(self._description_warnings(row, checked_size, canonical_uom))
            if conflicts:
                return GroupAValidationResult(
                    GroupAValidationStatus.REVIEW,
                    checked_size,
                    canonical_uom,
                    checked_pack,
                    tuple(issues),
                )

        if invalid:
            status = GroupAValidationStatus.INVALID
        elif auto_fix:
            status = GroupAValidationStatus.AUTO_FIX
        else:
            status = GroupAValidationStatus.VALID
        return GroupAValidationResult(
            status,
            checked_size,
            canonical_uom,
            checked_pack,
            tuple(issues),
        )

    def _legacy_issues(
        self,
        row: WorkbookRow,
        standard_size: Decimal,
        standard_uom: str,
    ) -> list[ValidationIssue]:
        product = row.product
        if blank(product.legacy_size) or blank(product.legacy_uom):
            return []
        proposal = self.comparison_engine.propose(product.legacy_size, product.legacy_uom)
        if proposal is None:
            return [ValidationIssue(
                "LEGACY_NOT_COMPARABLE", "legacy_uom", ValidationSeverity.INFO,
                "Legacy value has no deterministic comparison rule",
                product.legacy_uom,
            )]
        if proposal.standard_uom != standard_uom:
            return [ValidationIssue(
                "LEGACY_UOM_MISMATCH", "standard_uom", ValidationSeverity.WARNING,
                "Legacy conversion targets a different standardized UOM",
                standard_uom, proposal.standard_uom,
            )]
        if proposal.standard_size != standard_size:
            return [ValidationIssue(
                "LEGACY_SIZE_MISMATCH", "standard_size", ValidationSeverity.WARNING,
                "Existing standardized size differs from the rounded legacy conversion",
                str(standard_size), str(proposal.standard_size),
            )]
        return []

    @staticmethod
    def _description_warnings(
        row: WorkbookRow,
        standard_size: Decimal,
        standard_uom: str,
    ) -> list[ValidationIssue]:
        if standard_uom not in {"GM", "ML"}:
            return []
        expected_dimension = "WEIGHT" if standard_uom == "GM" else "VOLUME"
        issues: list[ValidationIssue] = []
        for evidence_field, product_attribute in DESCRIPTION_FIELDS:
            signal = extract_signal(
                evidence_field,
                getattr(row.product, product_attribute),
            )
            if signal is None:
                continue
            rounded_evidence = signal.value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            if signal.dimension != expected_dimension or (
                signal.value != standard_size and rounded_evidence != standard_size
            ):
                issues.append(ValidationIssue(
                    "DESCRIPTION_MEASUREMENT_MISMATCH",
                    evidence_field,
                    ValidationSeverity.WARNING,
                    "Explicit description measurement differs from existing K/L",
                    signal.fragment,
                    f"{standard_size} {standard_uom}",
                ))
        return issues
