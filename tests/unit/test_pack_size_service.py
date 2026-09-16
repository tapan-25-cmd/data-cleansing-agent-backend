from decimal import Decimal

import pytest

from app.domain.product import InputProduct
from app.services.pack_size_service import PackSizeService, PackStatus


def product(**changes) -> InputProduct:
    values = {
        "row_number": 2,
        "item_no": "000001",
        "department": "03_Grocery 2",
    }
    values.update(changes)
    return InputProduct(**values)


@pytest.mark.parametrize(
    ("description", "expected", "pattern"),
    [
        ("JUICE 6 X 500ML", "6", "COUNT_X_MEASUREMENT"),
        ("JUICE 500ML x 6", "6", "MEASUREMENT_X_COUNT"),
        ("PACK OF 10 BOTTLES", "10", "PACK_OF_COUNT"),
        ("12x330 ml CANS", "12", "COUNT_X_MEASUREMENT"),
        ("24 CANS 330ML", "24", "COUNT_CONTAINER_MEASUREMENT"),
        ("4 PK x 250 GM", "4", "COUNT_PACK"),
        ("6 PACK", "6", "COUNT_PACK"),
    ],
)
def test_explicit_pack_patterns_are_deterministic(description, expected, pattern):
    result = PackSizeService().assess(product(item_desc_eng=description))
    assert result.status == PackStatus.DETERMINISTIC_PROPOSAL
    assert result.pack_size == Decimal(expected)
    assert result.pattern_id == pattern
    assert result.evidence.fragment in description


def test_valid_existing_pack_wins_over_description():
    result = PackSizeService().assess(product(
        raw_standard_pack_size=8,
        standard_pack_size="8",
        item_desc_eng="6 X 500ML",
    ))
    assert result.status == PackStatus.EXISTING_VALID
    assert result.pack_size == Decimal("8")


def test_numeric_text_pack_is_normalized_without_inference():
    result = PackSizeService().assess(product(raw_standard_pack_size=" 6 "))
    assert result.status == PackStatus.NORMALIZE_EXISTING
    assert result.pack_size == Decimal("6")


@pytest.mark.parametrize("value", [0, -1, 1.5, "abc", "=1+1", "#VALUE!"])
def test_invalid_existing_pack_can_be_recovered_from_explicit_text(value):
    result = PackSizeService().assess(product(
        raw_standard_pack_size=value,
        item_desc_eng="6 X 500ML",
    ))
    assert result.status == PackStatus.DETERMINISTIC_PROPOSAL
    assert result.pack_size == Decimal("6")
    assert result.invalid_existing is True


def test_conflicting_descriptions_do_not_choose_a_pack_size():
    result = PackSizeService().assess(product(
        item_desc_eng="6 X 500ML",
        web_description_eng="PACK OF 12",
    ))
    assert result.status == PackStatus.CONFLICT
    assert result.pack_size is None
    assert {candidate.pack_size for candidate in result.candidates} == {
        Decimal("6"), Decimal("12")
    }


@pytest.mark.parametrize("description", ["WATER 500ML", "MODEL 2026", "SIZE 10"])
def test_unrelated_numbers_do_not_become_pack_sizes(description):
    result = PackSizeService().assess(product(item_desc_eng=description))
    assert result.status == PackStatus.NOT_FOUND
    assert result.pack_size is None


@pytest.mark.parametrize("description", ["ASSORTED 4'S", "MULTI PACK", "CASE SIZE UNKNOWN"])
def test_pack_clues_without_safe_pattern_are_agent_candidates(description):
    result = PackSizeService().assess(product(item_desc_eng=description))
    assert result.status == PackStatus.NEEDS_AGENT


def test_decimal_pack_pattern_is_not_partially_parsed():
    result = PackSizeService().assess(product(item_desc_eng="PACK OF 1.5"))
    assert result.status == PackStatus.NEEDS_AGENT
    assert result.pack_size is None


def test_bare_parenthetical_piece_count_is_not_deterministically_filled():
    result = PackSizeService().assess(product(
        item_desc_eng="IBN ABL BRAIS SAU(4PCS) 200GM"
    ))
    assert result.status == PackStatus.NEEDS_AGENT
    assert result.pack_size is None


def test_pack_agent_request_carries_context_but_not_brand_fields():
    request = PackSizeService.agent_request(
        product(item_brand_eng="BRAND 12", item_desc_eng="ASSORTED 4'S"),
        Decimal("500"),
        "ml",
    )
    assert request.task == "PACK_ONLY"
    assert request.known_measurement.value == Decimal("500")
    assert request.known_measurement.uom == "ML"
    assert request.item_brand_eng is None
