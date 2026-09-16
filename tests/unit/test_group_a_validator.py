from decimal import Decimal

import pytest

from app.domain.product import InputProduct
from app.rules.registry import load_default_registry
from app.services.excel_reader import FIELD_MAP, WorkbookRow
from app.services.group_a_validator import (
    GroupAValidationStatus,
    GroupAValidator,
)
from app.services.rule_engine import RuleEngine


def validator() -> GroupAValidator:
    return GroupAValidator(RuleEngine(load_default_registry(), 0))


def candidate(**changes) -> WorkbookRow:
    values = {
        "row_number": 2,
        "item_no": "000123",
        "department": "03_Grocery 2",
        "legacy_size": "500",
        "legacy_uom": "ML",
        "standard_size": "500",
        "standard_uom": "ML",
        "standard_pack_size": "1",
        "raw_standard_size": 500,
        "raw_standard_uom": "ML",
        "raw_standard_pack_size": 1,
    }
    values.update(changes)
    product = InputProduct(**values)
    raw = {
        FIELD_MAP["standard_size"]: product.raw_standard_size,
        FIELD_MAP["standard_uom"]: product.raw_standard_uom,
        FIELD_MAP["standard_pack_size"]: product.raw_standard_pack_size,
    }
    return WorkbookRow(row_number=product.row_number, raw=raw, product=product)


def issue_codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


def test_canonical_positive_numeric_fields_are_valid_and_preserved():
    result = validator().validate(candidate(
        standard_size="500.5",
        raw_standard_size=500.5,
        legacy_size=None,
        legacy_uom=None,
    ))
    assert result is not None
    assert result.status == GroupAValidationStatus.VALID
    assert result.standard_size == Decimal("500.5")
    assert result.normalization_proposal()["standard_size"] == "500.5"
    assert result.as_dict()["policy_version"] == "group-a-validation-v1"
    assert len(result.as_dict()["alias_checksum"]) == 64


@pytest.mark.parametrize("raw_uom", ["ml", "mL", "milliliter", "MILLILITRES", " ML "])
def test_known_uom_aliases_require_deterministic_normalization(raw_uom):
    result = validator().validate(candidate(
        raw_standard_uom=raw_uom,
        standard_uom=str(raw_uom).strip().upper(),
        standard_size="500.5",
        raw_standard_size=500.5,
    ))
    assert result is not None
    assert result.status == GroupAValidationStatus.AUTO_FIX
    assert result.standard_uom == "ML"
    assert result.normalization_proposal() == {
        "standard_size": "500.5", "standard_uom": "ML", "standard_pack_size": "1"
    }
    assert "UOM_CANONICALIZATION" in issue_codes(result)


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"raw_standard_size": 0, "standard_size": "0"}, "NOT_POSITIVE"),
        ({"raw_standard_size": -1, "standard_size": "-1"}, "NOT_POSITIVE"),
        ({"raw_standard_size": "ABC", "standard_size": "ABC"}, "NOT_NUMERIC"),
        ({"raw_standard_size": "=ROUND(1.2,0)", "standard_size": "=ROUND(1.2,0)"}, "FORMULA_NOT_ALLOWED"),
        ({"raw_standard_pack_size": 0, "standard_pack_size": "0"}, "NOT_POSITIVE"),
        ({"raw_standard_pack_size": 1.5, "standard_pack_size": "1.5"}, "NOT_WHOLE_NUMBER"),
    ],
)
def test_invalid_k_or_m_never_qualifies_for_group_a(changes, code):
    result = validator().validate(candidate(**changes))
    assert result is not None
    assert result.status == GroupAValidationStatus.INVALID
    assert code in issue_codes(result)


def test_numeric_text_is_safe_auto_fix_instead_of_trusted_a():
    result = validator().validate(candidate(
        raw_standard_size="500",
        raw_standard_pack_size="1",
    ))
    assert result is not None
    assert result.status == GroupAValidationStatus.AUTO_FIX
    assert "NUMERIC_TEXT_NORMALIZATION" in issue_codes(result)


def test_duplicate_item_number_is_invalid():
    result = validator().validate(candidate(), duplicate_item_number=True)
    assert result is not None
    assert result.status == GroupAValidationStatus.INVALID
    assert "DUPLICATE_ITEM_NUMBER" in issue_codes(result)


def test_bilingual_measurement_conflict_routes_to_review():
    result = validator().validate(candidate(
        item_desc_eng="JUICE 500ML",
        item_desc_local="果汁 1L",
    ))
    assert result is not None
    assert result.status == GroupAValidationStatus.REVIEW
    assert "BILINGUAL_DESCRIPTION_CONFLICT" in issue_codes(result)


def test_legacy_mismatch_is_recorded_as_warning_without_rewriting_a():
    result = validator().validate(candidate(
        legacy_size="1",
        legacy_uom="LT",
    ))
    assert result is not None
    assert result.status == GroupAValidationStatus.VALID
    assert result.has_warnings
    assert "LEGACY_SIZE_MISMATCH" in issue_codes(result)


def test_incomplete_standard_fields_are_not_owned_by_group_a_validator():
    result = validator().validate(candidate(
        raw_standard_pack_size=None,
        standard_pack_size=None,
    ))
    assert result is None
