from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from hashlib import sha256
import json
from typing import Any

from app.services.discrepancy_service import (
    DiscrepancyReport,
    analyze_discrepancies,
    matches_value,
    packaging_levels,
)
from app.services.excel_reader import FIELD_MAP, WorkbookRow
from app.services.normalization import blank
from app.services.packaging_expression_service import (
    extract_packaging_expressions,
    matching_interpretations,
)
from app.services.rule_engine import RuleEngine


GROUP_A_VALIDATION_VERSION = "group-a-validation-v2"
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
        discrepancies: DiscrepancyReport | None = None,
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
            report = discrepancies or analyze_discrepancies(row.product)
            # Only an explicit bilingual *measurement* conflict disqualifies a row
            # from Group A. A bilingual count conflict is a packaging-level
            # question that the result ledger raises for review without
            # re-classifying the row.
            conflicts = [
                conflict for conflict in report.bilingual_conflicts
                if conflict["aspect"] == "MEASUREMENT"
            ]
            for conflict in conflicts:
                issues.append(ValidationIssue(
                    "BILINGUAL_DESCRIPTION_CONFLICT",
                    str(conflict.get("pair") or "description"),
                    ValidationSeverity.ERROR,
                    "Paired description fields contain conflicting explicit measurements",
                    "; ".join(
                        f"{signal['fragment']} ({signal['field']})"
                        for signal in conflict["left_signals"]
                    ),
                    "; ".join(
                        f"{signal['fragment']} ({signal['field']})"
                        for signal in conflict["right_signals"]
                    ),
                ))
            packaging_expressions = extract_packaging_expressions(row.product)
            supported_packaging_fields: set[str] = set()
            ambiguous_packaging = False
            for expression in packaging_expressions:
                matches = matching_interpretations(
                    expression,
                    standard_size=checked_size,
                    standard_uom=canonical_uom,
                    standard_pack_size=checked_pack,
                )
                if matches:
                    supported_packaging_fields.add(expression.field)
                else:
                    ambiguous_packaging = True
                    issues.append(ValidationIssue(
                        "PACKAGING_HIERARCHY_AMBIGUOUS",
                        expression.field,
                        ValidationSeverity.WARNING,
                        "Existing K/L/M does not match an approved interpretation of the package expression",
                        expression.as_dict(),
                        list(expression.candidates()),
                    ))
            # English/local descriptions are a semantic pair. If a structured
            # expression in one side already explains K/L/M, suppress the
            # first-measurement warning on its translated partner as well.
            # A real value conflict is still caught above by the discrepancy engine.
            if supported_packaging_fields & {"web_description_eng", "web_description_chi"}:
                supported_packaging_fields.update({"web_description_eng", "web_description_chi"})
            if supported_packaging_fields & {"item_desc_eng", "item_desc_local_lang"}:
                supported_packaging_fields.update({"item_desc_eng", "item_desc_local_lang"})
            issues.extend(self._description_warnings(
                report,
                checked_size,
                canonical_uom,
                checked_pack,
                excluded_fields=supported_packaging_fields,
            ))
            if conflicts or ambiguous_packaging:
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
        # Group B conversions intentionally use Excel-style nearest-whole
        # rounding. Group A validation must not apply that rounding to an
        # identity comparison (for example 4.5 GM -> GM), otherwise a correct
        # existing decimal becomes a false 5 GM mismatch.
        expected_size = (
            proposal.raw_target
            if proposal.source_uom == proposal.target_uom
            and proposal.factor == Decimal("1")
            else proposal.standard_size
        )
        if expected_size != standard_size:
            difference = abs(expected_size - standard_size)
            identity = (
                proposal.source_uom == proposal.target_uom
                and proposal.factor == Decimal("1")
            )
            if not identity and difference <= Decimal("1"):
                return [ValidationIssue(
                    "ROUNDING_ONLY_VARIANCE",
                    "standard_size",
                    ValidationSeverity.INFO,
                    "Existing standardized size is within the approved one-unit conversion tolerance",
                    str(standard_size),
                    str(expected_size),
                )]
            return [ValidationIssue(
                "SIGNIFICANT_LEGACY_SIZE_MISMATCH", "standard_size", ValidationSeverity.WARNING,
                (
                    "Existing standardized size differs from the exact legacy value"
                    if identity
                    else "Existing standardized size differs from the rounded legacy conversion"
                ),
                str(standard_size), str(expected_size),
            )]
        return []

    @staticmethod
    def _description_warnings(
        report: DiscrepancyReport,
        standard_size: Decimal,
        standard_uom: str,
        standard_pack_size: Decimal,
        *,
        excluded_fields: set[str] | None = None,
    ) -> list[ValidationIssue]:
        if standard_uom not in {"GM", "ML"}:
            return []
        expected_dimension = "WEIGHT" if standard_uom == "GM" else "VOLUME"
        # A description may state the per-unit size or the whole-pack total, so
        # both K and K x M are acceptable readings of the existing values.
        accepted = {standard_size, standard_size * standard_pack_size}
        issues: list[ValidationIssue] = []
        excluded_fields = excluded_fields or set()
        for evidence_field, signals in report.signals.items():
            if evidence_field in excluded_fields or not signals.measurements:
                continue
            # Every measurement in the field is considered, at every packaging
            # level the same text states, before a mismatch is reported.
            converted = any(
                signal.source_uom != signal.uom for signal in signals.measurements
            )
            supported = any(
                dimension == expected_dimension
                and matches_value(value, target, converted=converted)
                for dimension, value in packaging_levels(
                    signals.measurements, signals.counts,
                )
                for target in accepted
            )
            if not supported:
                issues.append(ValidationIssue(
                    "DESCRIPTION_MEASUREMENT_MISMATCH",
                    evidence_field,
                    ValidationSeverity.WARNING,
                    "Explicit description measurement differs from existing K/L",
                    "; ".join(signal.fragment for signal in signals.measurements),
                    f"{standard_size} {standard_uom}",
                ))
        return issues
