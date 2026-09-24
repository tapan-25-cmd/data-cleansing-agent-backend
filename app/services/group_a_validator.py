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
    within_conversion_tolerance,
    packaging_levels,
    extract_field_signals,
)
from app.domain.product import InputProduct
from app.services.excel_reader import FIELD_MAP, WorkbookRow
from app.services.normalization import blank
from app.services.packaging_expression_service import (
    extract_packaging_expressions,
    matching_interpretations,
)
from app.services.rule_engine import RuleEngine
from app.services.klm_reconciliation_service import (
    LegacyAssessment,
    KLMReconciliationService,
    LegacyRelationship,
)


GROUP_A_VALIDATION_VERSION = "group-a-validation-v5"
# (field name used for evidence, attribute on InputProduct)
_DESCRIPTION_FIELDS = (
    ("item_desc_eng", "item_desc_eng"),
    ("item_desc_local_lang", "item_desc_local"),
    ("web_description_eng", "web_description_eng"),
    ("web_description_chi", "web_description_chi"),
)
_FIELD_NAMES = {
    "item_desc_eng": "item description, English",
    "item_desc_local_lang": "item description, local language",
    "web_description_eng": "web description, English",
    "web_description_chi": "web description, local language",
}
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
    # Complete values a linked suggestion proposes together, e.g. size and pack.
    proposed: dict[str, str] | None = None

    def as_dict(self) -> dict[str, object | None]:
        return {
            "code": self.code,
            "field": self.field,
            "severity": self.severity.value,
            "message": self.message,
            "current_value": _json_value(self.current_value),
            "expected_value": _json_value(self.expected_value),
            "proposed": dict(self.proposed) if self.proposed else None,
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
        self.klm_reconciliation = KLMReconciliationService(comparison_engine)

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
            issues.extend(self._legacy_issues(
                row, checked_size, canonical_uom, checked_pack,
            ))
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
            issues.extend(self._pack_count_notes(
                row.product, checked_size, canonical_uom, checked_pack,
            ))
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
        standard_pack_size: Decimal,
    ) -> list[ValidationIssue]:
        product = row.product
        if blank(product.legacy_size) or blank(product.legacy_uom):
            return []
        assessment = self.klm_reconciliation.assess_legacy(
            legacy_size=product.legacy_size,
            legacy_uom=product.legacy_uom,
            standard_size=standard_size,
            standard_uom=standard_uom,
            standard_pack_size=standard_pack_size,
        )
        if assessment.relationship == LegacyRelationship.NOT_COMPARABLE:
            return [ValidationIssue(
                "LEGACY_NOT_COMPARABLE", "legacy_uom", ValidationSeverity.INFO,
                (
                    f"We could not double-check this value against the legacy data, because "
                    f"the legacy unit {product.legacy_uom} has no agreed conversion. The Excel "
                    "value was kept as it is."
                ),
                product.legacy_uom,
            )]
        if assessment.relationship in {
            LegacyRelationship.UOM_MISMATCH, LegacyRelationship.SIGNIFICANT_MISMATCH,
        }:
            confirmed = self._description_confirmation(
                product, standard_size, standard_uom, standard_pack_size,
                legacy_unit=assessment.expected_uom,
            )
            if confirmed is not None:
                return [confirmed]
        if assessment.relationship == LegacyRelationship.UOM_MISMATCH:
            return [ValidationIssue(
                "LEGACY_UOM_MISMATCH", "standard_uom", ValidationSeverity.WARNING,
                "Legacy conversion targets a different standardized UOM",
                standard_uom, assessment.expected_uom,
            )]
        if assessment.relationship in {
            LegacyRelationship.TOTAL_MATCH,
            LegacyRelationship.TOTAL_ROUNDING_MATCH,
        }:
            return [ValidationIssue(
                "LEGACY_TOTAL_CONSISTENT", "standard_size", ValidationSeverity.INFO,
                (
                    "Legacy value matches the existing whole-pack total (K × M); "
                    "it is not a replacement for unit size K"
                ),
                f"{standard_size} {standard_uom} × {standard_pack_size}",
                f"{assessment.expected_value} {assessment.expected_uom}",
            )]
        if assessment.relationship == LegacyRelationship.UNIT_ROUNDING_MATCH:
            return [ValidationIssue(
                "ROUNDING_ONLY_VARIANCE", "standard_size", ValidationSeverity.INFO,
                "Existing standardized size is within the approved conversion tolerance (1 unit or 1%)",
                str(standard_size), str(assessment.expected_value),
            )]
        if assessment.relationship == LegacyRelationship.SIGNIFICANT_MISMATCH:
            linked = self._linked_suggestion(
                product, standard_size, standard_uom, standard_pack_size, assessment,
            )
            if linked is not None:
                return [linked]
            return [ValidationIssue(
                "SIGNIFICANT_LEGACY_SIZE_MISMATCH", "standard_size", ValidationSeverity.WARNING,
                (
                    "Existing standardized size differs from the exact legacy value"
                    if assessment.exact_identity
                    else "Existing standardized size differs from the rounded legacy conversion"
                ),
                str(standard_size), str(assessment.expected_value),
            )]
        return []

    @staticmethod
    def _description_counts(product: InputProduct) -> dict[Decimal, list[str]]:
        counts: dict[Decimal, list[str]] = {}
        for field, attribute in _DESCRIPTION_FIELDS:
            for count in extract_field_signals(field, getattr(product, attribute)).counts:
                counts.setdefault(Decimal(count.value), []).append(
                    f"{count.fragment} ({_FIELD_NAMES[field]})"
                )
        return counts

    def _linked_suggestion(
        self,
        product: InputProduct,
        standard_size: Decimal,
        standard_uom: str,
        standard_pack_size: Decimal,
        assessment: LegacyAssessment,
    ) -> ValidationIssue | None:
        """Legacy size × a count in the description equals Excel's total.

        Excel 550 GM × 1, legacy 55 GM, description `\\10`: 55 × 10 = 550. The legacy
        is the size of one pack and the description gives the number of packs, so
        the suggestion is both fields together, with the total unchanged. It is
        still a review: the representation (one pack of ten versus ten packs) is
        a business choice the text alone does not settle. A size-only suggestion
        here would silently shrink the total to 55.
        """
        expected = assessment.expected_value
        if expected is None or assessment.expected_uom != standard_uom:
            return None
        total = standard_size * standard_pack_size
        for count, fragments in sorted(self._description_counts(product).items()):
            if count <= Decimal("1") or count == standard_pack_size:
                continue
            if expected * count == total:
                return ValidationIssue(
                    "LINKED_SIZE_AND_PACK_SUGGESTION", "standard_size", ValidationSeverity.WARNING,
                    (
                        f"The legacy {product.legacy_size} {product.legacy_uom} is one pack and "
                        f"the description counts {count} packs: {expected} {standard_uom} × "
                        f"{count} = {total} {standard_uom}, the same total Excel has as "
                        f"{standard_size} {standard_uom} × {standard_pack_size}."
                    ),
                    f"{standard_size} {standard_uom} × {standard_pack_size}",
                    "; ".join(dict.fromkeys(fragments)),
                    proposed={
                        "standard_size": format(expected.normalize(), "f"),
                        "standard_uom": standard_uom,
                        "standard_pack_size": format(count.normalize(), "f"),
                    },
                )
        return None

    @staticmethod
    def _pack_count_notes(
        product: InputProduct,
        standard_size: Decimal,
        standard_uom: str,
        standard_pack_size: Decimal,
    ) -> list[ValidationIssue]:
        """The description states Excel's unit size next to a count that is not
        Excel's pack size (`CASE 25 X 120GM` against 120 GM × 50). A note only:
        which source is right is a business question, and the count may be a
        nested level (`3'S CASE 16 X 185GM` states 16, so it is not raised)."""
        dimension = {"GM": "WEIGHT", "ML": "VOLUME"}.get(standard_uom)
        if dimension is None:
            return []
        notes: list[ValidationIssue] = []
        for pair in (("item_desc_eng", "item_desc_local"), ("web_description_eng", "web_description_chi")):
            supports_unit = False
            counts: dict[Decimal, list[str]] = {}
            for attribute in pair:
                field = next(f for f, a in _DESCRIPTION_FIELDS if a == attribute)
                signals = extract_field_signals(field, getattr(product, attribute))
                converted = any(m.source_uom != m.uom for m in signals.measurements)
                if any(
                    m.dimension == dimension and matches_value(m.value, standard_size, converted=converted)
                    for m in signals.measurements
                ):
                    supports_unit = True
                for count in signals.counts:
                    counts.setdefault(Decimal(count.value), []).append(
                        f"{count.fragment} ({_FIELD_NAMES[field]})"
                    )
            if not supports_unit or not counts or standard_pack_size in counts:
                continue
            stated = ", ".join(format(c.normalize(), "f") for c in sorted(counts))
            notes.append(ValidationIssue(
                "DESCRIPTION_PACK_COUNT_DIFFERS", "standard_pack_size", ValidationSeverity.WARNING,
                (
                    f"The description states the unit size {standard_size} {standard_uom} "
                    f"with a count of {stated}, while Excel has a pack size of "
                    f"{standard_pack_size}. Nothing was changed and nothing is suggested: "
                    "a person confirms which is current."
                ),
                format(standard_pack_size.normalize(), "f"),
                "; ".join(dict.fromkeys(f for fs in counts.values() for f in fs)),
            ))
            break  # one note per row is enough
        return notes

    @staticmethod
    def _description_confirmation(
        product: InputProduct,
        standard_size: Decimal,
        standard_uom: str,
        standard_pack_size: Decimal,
        *,
        legacy_unit: str | None,
    ) -> ValidationIssue | None:
        """The product's own description states the values Excel already has.

        The legacy value then does not contradict Excel; most often it counts the
        package (1 PK) where Excel counts what is inside (6 EA, or 500 ML × 2).
        The row is confirmed with a note instead of being sent to a person. Two
        shapes are accepted, both requiring the description to be explicit:

        - pieces: Excel says N EA × 1, the description states the count N, and the
          legacy is either a single package (value 1) or a weight/volume, which
          measures the product differently rather than counting it differently;
        - measured: the description states Excel's unit size, and either the legacy
          is a single package with M = 1, or the description also states M.

        A description that states only the size while the legacy carries a count
        (legacy 50 PC, Excel 1 GM × 1, text 1G) is not a confirmation: the legacy
        count may be the missing pack size, so that row still goes to review.
        """
        dimension = {"GM": "WEIGHT", "ML": "VOLUME"}.get(standard_uom)
        try:
            legacy_value = Decimal(str(product.legacy_size).strip())
        except (InvalidOperation, ValueError):
            legacy_value = None
        single_package = legacy_value == Decimal("1")
        legacy_measures = legacy_unit in {"GM", "ML"}
        size_fragments: list[str] = []
        count_fragments: dict[Decimal, list[str]] = {}
        for field, attribute in _DESCRIPTION_FIELDS:
            signals = extract_field_signals(field, getattr(product, attribute))
            converted = any(m.source_uom != m.uom for m in signals.measurements)
            for measurement in signals.measurements:
                if dimension and measurement.dimension == dimension and matches_value(
                    measurement.value, standard_size, converted=converted,
                ):
                    size_fragments.append(f"{measurement.fragment} ({_FIELD_NAMES[field]})")
            for count in signals.counts:
                count_fragments.setdefault(Decimal(count.value), []).append(
                    f"{count.fragment} ({_FIELD_NAMES[field]})"
                )
        excel = f"{standard_size} {standard_uom} × {standard_pack_size}"
        legacy = f"{product.legacy_size} {product.legacy_uom}".strip()
        if (
            standard_uom == "EA" and standard_pack_size == Decimal("1")
            and (single_package or legacy_measures) and standard_size in count_fragments
        ):
            return ValidationIssue(
                "DESCRIPTION_CONFIRMS_PIECE_COUNT", "standard_size", ValidationSeverity.INFO,
                (
                    f"The description states {standard_size} pieces, which matches Excel. "
                    + (
                        f"The legacy {legacy} is the package, not one piece."
                        if single_package
                        else f"The legacy {legacy} measures the product by weight or volume "
                        "instead of counting pieces; it does not contradict the count."
                    )
                ),
                excel, "; ".join(dict.fromkeys(count_fragments[standard_size])),
            )
        if dimension and size_fragments:
            pack_confirmed = (
                standard_pack_size > Decimal("1") and standard_pack_size in count_fragments
            )
            if pack_confirmed or (standard_pack_size == Decimal("1") and single_package):
                fragments = list(size_fragments)
                if pack_confirmed:
                    fragments += count_fragments[standard_pack_size]
                return ValidationIssue(
                    "DESCRIPTION_CONFIRMS_UNIT_SIZE", "standard_size", ValidationSeverity.INFO,
                    (
                        f"The description states {excel}, which matches Excel. "
                        f"The legacy {legacy} does not contradict it."
                    ),
                    excel, "; ".join(dict.fromkeys(fragments)),
                )
        return None

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
