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
    assert result.as_dict()["policy_version"] == "group-a-validation-v2"
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
    assert "SIGNIFICANT_LEGACY_SIZE_MISMATCH" in issue_codes(result)


def test_identity_legacy_comparison_preserves_exact_decimal_without_group_b_rounding():
    result = validator().validate(candidate(
        legacy_size="4.5",
        legacy_uom="GM",
        standard_size="4.5",
        raw_standard_size=4.5,
        standard_uom="GM",
        raw_standard_uom="GM",
    ))
    assert result is not None
    assert result.status == GroupAValidationStatus.VALID
    assert "SIGNIFICANT_LEGACY_SIZE_MISMATCH" not in issue_codes(result)


def test_converted_legacy_comparison_still_uses_nearest_whole_policy():
    result = validator().validate(candidate(
        legacy_size="17.1",
        legacy_uom="OZ",
        standard_size="485",
        raw_standard_size=485,
        standard_uom="GM",
        raw_standard_uom="GM",
    ))
    assert result is not None
    assert "SIGNIFICANT_LEGACY_SIZE_MISMATCH" not in issue_codes(result)


def test_one_unit_conversion_difference_is_rounding_only():
    result = validator().validate(candidate(
        legacy_size="17.1",
        legacy_uom="OZ",
        standard_size="484",
        raw_standard_size=484,
        standard_uom="GM",
        raw_standard_uom="GM",
    ))
    assert result is not None
    assert "ROUNDING_ONLY_VARIANCE" in issue_codes(result)
    assert "SIGNIFICANT_LEGACY_SIZE_MISMATCH" not in issue_codes(result)


def test_material_conversion_difference_is_significant():
    result = validator().validate(candidate(
        legacy_size="12.3",
        legacy_uom="OZ",
        standard_size="375",
        raw_standard_size=375,
        standard_uom="GM",
        raw_standard_uom="GM",
    ))
    assert result is not None
    issue = next(
        issue for issue in result.issues
        if issue.code == "SIGNIFICANT_LEGACY_SIZE_MISMATCH"
    )
    assert issue.current_value == "375"
    assert issue.expected_value == "349"


def test_incomplete_standard_fields_are_not_owned_by_group_a_validator():
    result = validator().validate(candidate(
        raw_standard_pack_size=None,
        standard_pack_size=None,
    ))
    assert result is None


def test_approved_outer_total_packaging_interpretation_does_not_create_false_warning():
    result = validator().validate(candidate(
        legacy_size="450",
        legacy_uom="GM",
        standard_size="450",
        raw_standard_size=450,
        standard_uom="GM",
        raw_standard_uom="GM",
        standard_pack_size="6",
        raw_standard_pack_size=6,
        web_description_eng="5 CASE/6 X 90GM",
        web_description_chi="產品原箱/6 X 90GM",
    ))
    assert result is not None
    assert result.status == GroupAValidationStatus.VALID
    assert "PACKAGING_HIERARCHY_AMBIGUOUS" not in issue_codes(result)
    assert "DESCRIPTION_MEASUREMENT_MISMATCH" not in issue_codes(result)


def test_unapproved_packaging_arithmetic_routes_group_a_to_review():
    result = validator().validate(candidate(
        legacy_size="450",
        legacy_uom="GM",
        standard_size="18",
        raw_standard_size=18,
        standard_uom="GM",
        raw_standard_uom="GM",
        standard_pack_size="50",
        raw_standard_pack_size=50,
        web_description_eng="5 CASE/10 X 90GM",
    ))
    assert result is not None
    assert result.status == GroupAValidationStatus.REVIEW
    assert "PACKAGING_HIERARCHY_AMBIGUOUS" in issue_codes(result)


def test_description_check_considers_every_measurement_in_the_field():
    # v1 compared only the first signal (10G) and raised a false mismatch.
    result = validator().validate(candidate(
        legacy_size="180", legacy_uom="GM", standard_size="180", standard_uom="GM",
        raw_standard_size=180, raw_standard_uom="GM",
        web_description_chi="鮮牛奶輕熟芝士(10GX18)180G",
    ))

    assert result.status == GroupAValidationStatus.VALID
    assert "DESCRIPTION_MEASUREMENT_MISMATCH" not in issue_codes(result)


def test_description_stating_the_whole_pack_total_supports_k_times_m():
    result = validator().validate(candidate(
        legacy_size="65", legacy_uom="ML", standard_size="65", standard_pack_size="6",
        raw_standard_size=65, raw_standard_pack_size=6,
        item_desc_local="草莓乳飲390毫升",
    ))

    assert "DESCRIPTION_MEASUREMENT_MISMATCH" not in issue_codes(result)


def test_local_language_measurement_is_checked_against_existing_values():
    result = validator().validate(candidate(
        legacy_size="540", legacy_uom="GM", standard_size="540", standard_uom="GM",
        raw_standard_size=540, raw_standard_uom="GM",
        web_description_chi="白菜豬肉水餃720克",
    ))

    assert result.status == GroupAValidationStatus.VALID
    mismatch = next(i for i in result.issues if i.code == "DESCRIPTION_MEASUREMENT_MISMATCH")
    assert (mismatch.field, mismatch.current_value) == ("web_description_chi", "720克")


def test_pack_count_conflict_does_not_reclassify_group_a():
    result = validator().validate(candidate(
        item_desc_eng="ICE BAR 9S", item_desc_local="雪條3支裝",
    ))

    assert result.status == GroupAValidationStatus.VALID
    assert "BILINGUAL_DESCRIPTION_CONFLICT" not in issue_codes(result)


def test_label_rounding_within_one_percent_is_not_a_mismatch():
    # D4: 5 LB is 2268 GM; labels print 2.27 kg, so people entered 2270.
    result = validator().validate(candidate(
        legacy_size="5", legacy_uom="LB", standard_size="2270", standard_uom="GM",
        raw_standard_size=2270, raw_standard_uom="GM",
    ))
    assert "ROUNDING_ONLY_VARIANCE" in issue_codes(result)
    assert "SIGNIFICANT_LEGACY_SIZE_MISMATCH" not in issue_codes(result)


def test_a_real_error_is_still_outside_the_tolerance():
    result = validator().validate(candidate(
        legacy_size="16", legacy_uom="OZ", standard_size="907", standard_uom="GM",
        raw_standard_size=907, raw_standard_uom="GM",
    ))
    assert "SIGNIFICANT_LEGACY_SIZE_MISMATCH" in issue_codes(result)
