"""Complete a half-filled row: some of size (K), unit (L) and pack (M) are filled, not all.

What is filled is kept. Each missing value is looked for, in this order:
    1. the old size and unit (I/J), converted by the unit table;
    2. the product's own description, when it states the value literally;
    3. for the pack size only: 1, when no count is written anywhere.
A value that is only suggested (the sources hint at it but do not settle it) or that
cannot be found at all sends the row to a person. Every such row carries a note that it
was partly filled in the workbook. No AI call is made here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.product import InputProduct, RuleProposal
from app.rules.registry import RuleRegistry
from app.services.discrepancy_service import extract_field_signals, matches_value
from app.services.group_a_validator import STANDARD_UOM_ALIASES
from app.services.guards import FLUID_OUNCE_UOM
from app.services.normalization import blank
from app.services.pack_size_service import PackAssessment, PackStatus
from app.services.rule_engine import RuleEngine

GAP_FILL_VERSION = "gap-fill-v1"
TEXT_FIELDS = (
    ("item_desc_eng", "item_desc_eng"), ("item_desc_local_lang", "item_desc_local"),
    ("web_description_eng", "web_description_eng"), ("web_description_chi", "web_description_chi"),
)
FIELD_WORDS = {"standard_size": "size", "standard_uom": "unit", "standard_pack_size": "pack size"}
DIMENSION = {"GM": "WEIGHT", "ML": "VOLUME"}


def _positive(value: object, *, whole: bool = False) -> Decimal | None:
    if blank(value):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number <= 0:
        return None
    if whole and number != number.to_integral_value():
        return None
    return number


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _issue(code: str, field: str, severity: str, message: str, current: object = None, expected: object = None) -> dict[str, Any]:
    return {"code": code, "field": field, "severity": severity, "message": message,
            "current_value": current, "expected_value": expected, "gap_fill_version": GAP_FILL_VERSION}


@dataclass
class GapFill:
    proposals: dict[str, str] = field(default_factory=dict)
    provenance: dict[str, dict[str, Any]] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)
    review: bool = False
    # Size, unit and pack to run the usual checks with, once size and unit are known.
    check_values: tuple[Decimal, str, Decimal] | None = None


class GapFillService:
    def __init__(self, rule_engine: RuleEngine, registry: RuleRegistry):
        self.rule_engine = rule_engine
        self.registry = registry

    # -- sources -------------------------------------------------------------
    def _legacy(self, product: InputProduct, unit: str | None = None) -> RuleProposal | None:
        """The old size converted by the table, optionally in a required unit. An ounce
        is read as a fluid ounce when a volume is required."""
        if blank(product.legacy_size) or blank(product.legacy_uom):
            return None
        proposal = self.rule_engine.propose(product.legacy_size, product.legacy_uom)
        if unit is None or (proposal and proposal.standard_uom == unit):
            return proposal
        if unit == "ML" and str(product.legacy_uom).strip().upper() in {"OZ", "FZ", "FLOZ", "FL OZ"}:
            fluid = self.rule_engine.propose(product.legacy_size, FLUID_OUNCE_UOM)
            if fluid and fluid.standard_uom == "ML":
                return fluid
        return None

    @staticmethod
    def _legacy_value(proposal: RuleProposal) -> Decimal:
        identity = proposal.source_uom == proposal.target_uom and proposal.factor == Decimal("1")
        return proposal.raw_target if identity else proposal.standard_size

    @staticmethod
    def _text_measurements(product: InputProduct) -> list[tuple[Any, bool, str]]:
        found = []
        for name, attribute in TEXT_FIELDS:
            signals = extract_field_signals(name, getattr(product, attribute))
            for measurement in signals.measurements:
                found.append((measurement, measurement.source_uom != measurement.uom, name))
        return found

    # -- the fill ------------------------------------------------------------
    def fill(self, product: InputProduct, pack: PackAssessment) -> GapFill:
        result = GapFill()
        has_k, has_l, has_m = (not blank(product.standard_size), not blank(product.standard_uom),
                               not blank(product.standard_pack_size))
        k = _positive(product.standard_size)
        m = _positive(product.standard_pack_size, whole=True)
        unit = None
        unit_source_rule = None
        if has_l:
            raw_unit = str(product.standard_uom).strip().upper()
            unit = STANDARD_UOM_ALIASES.get(raw_unit)
            if unit is None and self.registry.get(raw_unit):
                rule = self.registry.get(raw_unit)
                unit, unit_source_rule = rule.target_uom, rule.rule_id
            elif unit is not None and raw_unit != unit:
                unit_source_rule = "STANDARD_FIELDS_CANONICALIZATION"

        present = [FIELD_WORDS[f] for f, h in (("standard_size", has_k), ("standard_uom", has_l), ("standard_pack_size", has_m)) if h]
        missing = [FIELD_WORDS[f] for f, h in (("standard_size", has_k), ("standard_uom", has_l), ("standard_pack_size", has_m)) if not h]
        result.issues.append(_issue(
            "PARTLY_FILLED_ROW", "standard_size", "INFO",
            f"This row was partly filled in the workbook: it had the {' and '.join(present)} but not the "
            f"{' or '.join(missing)}.", ", ".join(present), ", ".join(missing),
        ))

        unusable = [name for name, bad in (("size", has_k and k is None), ("unit", has_l and unit is None),
                                           ("pack size", has_m and m is None)) if bad]
        if unusable:
            result.issues.append(_issue(
                "UNUSABLE_VALUE", "standard_size", "REVIEW",
                f"The {' and '.join(unusable)} in Excel cannot be used (for example text where a number "
                "should be, or a unit that is not in the table), so the row was not completed.",
                ", ".join(unusable),
            ))
            result.review = True
            return result

        size = k
        # ---- unit missing, size present
        if has_k and not has_l:
            legacy = self._legacy(product)
            legacy_value = self._legacy_value(legacy) if legacy else None
            if legacy and legacy_value == k:
                unit = legacy.standard_uom
                self._propose(result, "standard_uom", unit, "GAP_FROM_LEGACY",
                              f"old size {product.legacy_size} {product.legacy_uom} = {_plain(k)} {unit}")
            elif legacy and m is not None and m > 1 and legacy_value == k * m:
                # The old size is the whole pack: size × pack gives it exactly.
                unit = legacy.standard_uom
                self._propose(result, "standard_uom", unit, "GAP_FROM_LEGACY",
                              f"old size {product.legacy_size} {product.legacy_uom} = {_plain(k)} {unit} × {_plain(m)}")
            else:
                units = {mm.uom for mm, converted, _ in self._text_measurements(product)
                         if matches_value(mm.value, k, converted=converted)}
                if len(units) == 1:
                    unit = units.pop()
                    self._propose(result, "standard_uom", unit, "GAP_FROM_TEXT", f"the description states {_plain(k)} {unit}")
                elif legacy:
                    unit = legacy.standard_uom
                    self._suggest(result, "standard_uom", unit,
                                  f"The old unit {product.legacy_uom} suggests {unit}, but the old size "
                                  f"{product.legacy_size} does not equal the size {_plain(k)} in Excel, so a person confirms the unit.")
                else:
                    self._not_found(result, "standard_uom", "Neither the old size nor the description states a unit for this size.")

        # ---- size missing, unit present
        if not has_k and unit is not None:
            if unit_source_rule:
                self._propose(result, "standard_uom", unit, unit_source_rule, f"{product.standard_uom} is written as {unit}")
            legacy = self._legacy(product, unit)
            other_kind = self._legacy(product) if legacy is None else None
            texts = sorted({mm.value for mm, _, _ in self._text_measurements(product) if mm.uom == unit})
            if legacy is not None:
                value = self._legacy_value(legacy)
                if m is None or m == 1:
                    size = value
                    self._propose(result, "standard_size", _plain(value), "GAP_FROM_LEGACY",
                                  f"old size {product.legacy_size} {product.legacy_uom} = {_plain(value)} {unit}")
                else:
                    size = value
                    self._suggest(result, "standard_size", _plain(value),
                                  f"The old size converts to {_plain(value)} {unit}, but Excel's pack size is "
                                  f"{_plain(m)}, so the old size may be the whole pack rather than one piece.")
            elif len(texts) == 1 and other_kind is None:
                size = texts[0]
                self._propose(result, "standard_size", _plain(size), "GAP_FROM_TEXT", f"the description states {_plain(size)} {unit}")
            elif len(texts) == 1:
                size = texts[0]
                self._suggest(result, "standard_size", _plain(size),
                              f"The description states {_plain(size)} {unit}, but the old size is a different kind "
                              f"of unit ({product.legacy_size} {product.legacy_uom}), so a person confirms it.")
            elif len(texts) > 1:
                self._not_found(result, "standard_size",
                                f"The description states more than one size in {unit} ({', '.join(_plain(t) for t in texts)}), so none is chosen.")
            else:
                self._not_found(result, "standard_size", "Neither the old size nor the description states a size in this unit.")

        # ---- pack missing
        if not has_m and size is not None and unit is not None:
            if pack.status == PackStatus.DETERMINISTIC_PROPOSAL and pack.pack_size is not None:
                m = pack.pack_size
                self._propose(result, "standard_pack_size", _plain(m), "GAP_PACK_FROM_TEXT",
                              f"the description says “{pack.evidence.fragment if pack.evidence else _plain(m)}”")
            elif pack.status in {PackStatus.CONFLICT, PackStatus.NEEDS_AGENT}:
                self._not_found(result, "standard_pack_size",
                                "The description mentions a count but does not settle the pack size, so a person decides.")
            else:
                legacy = self._legacy(product, unit)
                ratio = (self._legacy_value(legacy) / size) if legacy is not None and size else None
                if ratio is not None and ratio > 1 and ratio == ratio.to_integral_value():
                    m = ratio
                    self._suggest(result, "standard_pack_size", _plain(ratio),
                                  f"The old size {product.legacy_size} {product.legacy_uom} equals {_plain(ratio)} × "
                                  f"{_plain(size)} {unit}, which suggests a pack of {_plain(ratio)}; a person confirms it.")
                else:
                    m = Decimal("1")
                    self._propose(result, "standard_pack_size", "1", "SINGLE_ITEM_DEFAULT",
                                  "no pack count is written anywhere")
                    result.issues.append(_issue(
                        "PACK_SIZE_SINGLE_ITEM", "standard_pack_size", "INFO",
                        "No pack count is written in any description or in the legacy data, so the product is "
                        "recorded as a single item: pack size 1.", None, "1",
                    ))
        elif not has_m:
            self._not_found(result, "standard_pack_size", "The pack size cannot be worked out while the size or unit is missing.")

        if size is not None and unit is not None and m is not None:
            result.check_values = (size, unit, m)
        return result

    @staticmethod
    def _propose(result: GapFill, field_name: str, value: str, rule_id: str, evidence: str) -> None:
        result.proposals[field_name] = value
        result.provenance[field_name] = {"method": "RULE", "rule_id": rule_id, "evidence_text": evidence,
                                         "gap_fill_version": GAP_FILL_VERSION}

    @staticmethod
    def _suggest(result: GapFill, field_name: str, value: str, why: str) -> None:
        result.proposals[field_name] = value
        result.provenance[field_name] = {"method": "RULE", "rule_id": "GAP_SUGGESTED", "gap_fill_version": GAP_FILL_VERSION}
        result.issues.append(_issue("GAP_SUGGESTED", field_name, "REVIEW", why, None, value))
        result.review = True

    @staticmethod
    def _not_found(result: GapFill, field_name: str, why: str) -> None:
        result.issues.append(_issue("GAP_NOT_FOUND", field_name, "REVIEW",
                                    f"The {FIELD_WORDS[field_name]} is missing and could not be found. {why}"))
        result.review = True
