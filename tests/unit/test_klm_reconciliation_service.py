from decimal import Decimal

from app.rules.registry import load_default_registry
from app.services.klm_reconciliation_service import (
    KLMReconciliationService,
    LegacyRelationship,
)
from app.services.rule_engine import RuleEngine


def service() -> KLMReconciliationService:
    return KLMReconciliationService(RuleEngine(load_default_registry(), 0))


def assess(**changes):
    values = {
        "legacy_size": "350",
        "legacy_uom": "GM",
        "standard_size": Decimal("70"),
        "standard_uom": "GM",
        "standard_pack_size": Decimal("5"),
    }
    values.update(changes)
    return service().assess_legacy(**values)


def test_legacy_equal_to_k_times_m_is_classified_as_total_not_unit_replacement():
    result = assess()

    assert result.relationship == LegacyRelationship.TOTAL_MATCH
    assert result.legacy_looks_like_total
    assert result.existing_total == Decimal("350")


def test_legacy_equal_to_k_is_a_unit_match():
    result = assess(legacy_size="70")

    assert result.relationship == LegacyRelationship.UNIT_MATCH
    assert not result.legacy_looks_like_total


def test_pack_one_prefers_unit_match_because_unit_and_total_are_identical():
    result = assess(
        legacy_size="70",
        standard_pack_size=Decimal("1"),
    )

    assert result.relationship == LegacyRelationship.UNIT_MATCH


def test_converted_legacy_can_match_whole_pack_with_conversion_tolerance():
    result = assess(
        legacy_size="12.35",
        legacy_uom="OZ",
        standard_size=Decimal("70"),
        standard_pack_size=Decimal("5"),
    )

    assert result.relationship in {
        LegacyRelationship.TOTAL_MATCH,
        LegacyRelationship.TOTAL_ROUNDING_MATCH,
    }


def test_material_difference_remains_a_real_mismatch():
    result = assess(legacy_size="375")

    assert result.relationship == LegacyRelationship.SIGNIFICANT_MISMATCH
    assert result.expected_value == Decimal("375")


def test_different_dimension_is_not_treated_as_a_numeric_match():
    result = assess(legacy_size="350", legacy_uom="ML")

    assert result.relationship == LegacyRelationship.UOM_MISMATCH



def test_same_unit_legacy_within_tolerance_of_the_whole_pack_is_a_rounding_total_match():
    result = assess(
        legacy_size="380", legacy_uom="GM",
        standard_size=Decimal("63.4"), standard_pack_size=Decimal("6"),
    )
    assert result.relationship == LegacyRelationship.TOTAL_ROUNDING_MATCH


def test_same_unit_legacy_near_the_unit_size_still_needs_an_exact_match():
    result = assess(legacy_size="5", legacy_uom="GM", standard_size=Decimal("4.5"), standard_pack_size=Decimal("1"))
    assert result.relationship == LegacyRelationship.SIGNIFICANT_MISMATCH


def test_piece_counts_get_no_rounding_allowance():
    result = assess(legacy_size="1", legacy_uom="PC", standard_size=Decimal("2"), standard_uom="EA", standard_pack_size=Decimal("1"))
    assert result.relationship == LegacyRelationship.SIGNIFICANT_MISMATCH


def test_an_ounce_against_a_volume_is_read_as_a_fluid_ounce():
    # 32 OZ of vinegar is 946 ML: the same reading the conversion gives a liquid, so a row
    # the tool converted is not questioned when the cleansed workbook is read back.
    one = {"standard_pack_size": Decimal("1")}
    assert assess(legacy_size="32", legacy_uom="OZ", standard_size=Decimal("946"), standard_uom="ML", **one).relationship == LegacyRelationship.UNIT_MATCH
    assert assess(legacy_size="48", legacy_uom="OZ", standard_size=Decimal("1420"), standard_uom="ML", **one).relationship == LegacyRelationship.UNIT_MATCH
    # A real difference stays a difference, now with the fluid-ounce value to compare.
    prune = assess(legacy_size="32", legacy_uom="OZ", standard_size=Decimal("960"), standard_uom="ML", **one)
    assert (prune.relationship, prune.expected_value, prune.expected_uom) == (LegacyRelationship.SIGNIFICANT_MISMATCH, Decimal("946"), "ML")
    # Against a weight the ounce stays a weight ounce.
    assert assess(legacy_size="16", legacy_uom="OZ", standard_size=Decimal("454"), standard_uom="GM", **one).relationship == LegacyRelationship.UNIT_MATCH
