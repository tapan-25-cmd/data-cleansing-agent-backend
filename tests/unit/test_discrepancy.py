from decimal import Decimal

import pytest

from app.domain.product import InputProduct
from app.services.discrepancy_service import (
    analyze_discrepancies,
    extract_field_signals,
    find_discrepancies,
)


def product(**changes):
    values = {"row_number": 2, "item_no": "1", "department": "03_Grocery 2"}
    values.update(changes)
    return InputProduct(**values)


def measurements(text):
    return [(str(s.source_value), s.source_uom, s.value, s.fragment)
            for s in extract_field_signals("f", text).measurements]


def counts(text):
    return [signal.value for signal in extract_field_signals("f", text).counts]


def classifications(item):
    return [(d["scope"], d["aspect"], d["status"], d["classification"])
            for d in analyze_discrepancies(item).details]


def test_equivalent_units_do_not_conflict():
    item = product(item_desc_eng="JUICE 1000ML", item_desc_local="JUICE 1L")
    assert find_discrepancies(item) == []


def test_different_sizes_conflict_without_choosing_winner():
    item = product(item_desc_eng="JUICE 500ML", item_desc_local="JUICE 1L")
    details = find_discrepancies(item)
    assert len(details) == 1
    assert details[0]["left"]["value"] == "500"
    assert details[0]["right"]["value"] == "1000"
    assert details[0]["classification"] == "VALUE_CONFLICT"


def test_every_measurement_is_extracted_with_literal_offsets():
    text = "CHEESE 180G(10GX18)"
    signals = extract_field_signals("web_description_chi", text).measurements
    assert [(s.fragment, s.value) for s in signals] == [("180G", Decimal("180")), ("10G", Decimal("10"))]
    assert all(text[s.start:s.end] == s.fragment for s in signals)


@pytest.mark.parametrize(("text", "expected"), [
    ("蠔皇元貝鮑魚撈飯240克", [("240", "GM", Decimal("240"), "240克")]),
    ("雙重芝士迷你杯雪糕100毫升", [("100", "ML", Decimal("100"), "100毫升")]),
    ("提子汁飲品1.1公升", [("1.1", "L", Decimal("1100.0"), "1.1公升")]),
    ("牛油2公斤", [("2", "KG", Decimal("2000"), "2公斤")]),
    ("GOLD LABEL500MLX2", [("500", "ML", Decimal("500"), "500ML")]),
    ("INSTANTSOUP COMBO 320GX4PACKS", [("320", "G", Decimal("320"), "320G")]),
])
def test_local_language_and_glued_units_are_recognized(text, expected):
    assert measurements(text) == expected


@pytest.mark.parametrize("text", [
    "9CM TART RING", "0.6MM THINLY WRAPPED", "5 LAYERS CAKE", "CHATEAU MUSAR 2005", "3 GRAINS",
])
def test_non_measurements_are_ignored(text):
    assert measurements(text) == []


@pytest.mark.parametrize(("text", "expected"), [
    ("GRAND STRAWBERRY STICK 4SX70ML", [4]),
    ("士多啤梨雪條4件裝4x70毫升", [4, 4]),
    ("PB MANGO STK 4'S", [4]),
    ("YUNNAN BEEF R.VER\\5", [5]),
    ("Korean Udon 5 x", [5]),
    ("銀絲米粉五包裝", [5]),
    ("壽桃瑤柱麵十二個裝禮盒", [12]),
    ("金標孖裝", [2]),
    ("GOLD LABELX2", [2]),
    ("GOLD LABEL 500MLX2", [2]),
    ("FUKU INSTANT NOODLE 5 CASE/6 X 90GM", [5, 6]),
    ("TUNA SNACK CASE 24 X 1PC", [24, 1]),
    ("NESCAFE STICK 4X60", [4, 60]),
    ("6個月以上嬰兒米餅", []),
    ("10頭鮑魚", []),
])
def test_count_extraction(text, expected):
    assert counts(text) == expected


def test_any_shared_measurement_means_the_pair_agrees():
    item = product(
        web_description_eng="BAKED MILKY CHEESE CRAN 180G 18P",
        web_description_chi="鮮牛奶輕熟芝士蔓越莓180G(10GX18)",
    )
    assert classifications(item) == []


def test_converted_units_use_the_rounding_tolerance():
    item = product(item_desc_eng="BROWN SUGAR 16OZ", item_desc_local="冰片糖454克")
    assert classifications(item) == []


def test_pack_total_versus_per_unit_is_a_packaging_level_observation():
    item = product(item_desc_eng="NOODLE 6 X 90GM", item_desc_local="麵540克")
    assert classifications(item) == [
        ("BILINGUAL_PAIR", "MEASUREMENT", "OBSERVATION", "PACKAGING_LEVEL_DIFFERENCE"),
    ]
    assert find_discrepancies(item) == []


def test_one_populated_side_is_insufficient_data_not_a_conflict():
    item = product(item_desc_eng="JUICE 500ML", item_desc_local="果汁")
    report = analyze_discrepancies(item)
    assert classifications(item) == [
        ("BILINGUAL_PAIR", "MEASUREMENT", "INSUFFICIENT", "INSUFFICIENT_COMPARISON_DATA"),
    ]
    assert report.flagged is False


def test_dimension_conflict_and_likely_typo_are_classified():
    assert classifications(product(item_desc_eng="SAUCE 500GM", item_desc_local="醬500毫升")) == [
        ("BILINGUAL_PAIR", "MEASUREMENT", "CONFLICT", "DIMENSION_CONFLICT"),
    ]
    assert classifications(product(item_desc_eng="SAUCE 50GM", item_desc_local="醬500克")) == [
        ("BILINGUAL_PAIR", "MEASUREMENT", "CONFLICT", "LIKELY_TYPO"),
    ]


def test_ounce_against_volume_is_not_asserted_as_a_conflict():
    item = product(item_desc_eng="COLA 12OZ", item_desc_local="可樂355毫升")
    assert classifications(item) == [
        ("BILINGUAL_PAIR", "MEASUREMENT", "OBSERVATION", "FLUID_OUNCE_AMBIGUOUS"),
    ]


def test_bilingual_count_conflict_preserves_both_fragments():
    item = product(
        web_description_eng="KNORR DENSESOUP 4'S CASE 24 X 32GM",
        web_description_chi="家樂牌濃湯寶原箱424 X 32GM",
    )
    report = analyze_discrepancies(item)
    assert [d["classification"] for d in report.bilingual_conflicts] == ["COUNT_CONFLICT"]
    detail = report.bilingual_conflicts[0]
    assert [s["fragment"] for s in detail["left_signals"]] == ["4'S", "24 X "]
    assert [s["value"] for s in detail["right_signals"]] == [424]


def test_pairs_are_never_crossed():
    # Item description says 10, web description says 30, and the brand carries a
    # number too. Each language pair agrees with itself, so nothing is flagged.
    item = product(
        item_brand_eng="BRAND 500ML", item_brand_local="牌子500毫升",
        item_desc_eng="GRK STY YG STRAW 10S 200GM", item_desc_local="士啤梨燕麥乳酪10支裝200克",
        web_description_eng="YOG STRAW & OAT CASE/ 30X250GM", web_description_chi="乳酪原箱30X250克",
    )
    report = analyze_discrepancies(item)
    assert report.details == ()
    assert report.flagged is False


def test_only_the_three_language_pairs_are_compared():
    item = product(
        item_brand_eng="A 1KG", item_brand_local="甲2公斤",
        item_desc_eng="B 100ML", item_desc_local="乙200毫升",
        web_description_eng="C 5GM", web_description_chi="丙7克",
    )
    details = analyze_discrepancies(item).details
    assert [(d["scope"], d["pair"]) for d in details] == [
        ("BILINGUAL_PAIR", "brand"), ("BILINGUAL_PAIR", "item_desc"), ("BILINGUAL_PAIR", "web_desc"),
    ]
    compared = [
        {s["field"] for s in detail["left_signals"] + detail["right_signals"]}
        for detail in details
    ]
    assert compared == [
        {"item_brand_eng", "item_brand_local_lang"},
        {"item_desc_eng", "item_desc_local_lang"},
        {"web_description_eng", "web_description_chi"},
    ]
