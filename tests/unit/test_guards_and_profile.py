from decimal import Decimal

from app.services import guards
from app.services.category_profile import CategoryProfile
from app.services.discrepancy_service import extract_field_signals

MILK = {"section": "Milk", "subcategory": "Milk", "category": "Fresh Milk", "department": "Dairy"}
SAUCE = {"section": "Chilli Sauce", "subcategory": "Asian", "category": "Sauces", "department": "Grocery"}
FLOUR = {"section": "Plain Flour", "subcategory": "Flour", "category": "Baking", "department": "Grocery"}


def profile():
    rows = [(MILK, "ML", Decimal(size)) for size in (946, 1000, 1000, 1890, 250) * 4]
    rows += [(SAUCE, "ML", Decimal(300))] * 10 + [(SAUCE, "GM", Decimal(300))] * 10
    rows += [(FLOUR, "GM", Decimal(size)) for size in (500, 1000, 1000, 1500, 2270) * 4]
    rows += [(FLOUR, "EA", Decimal(1))] * 2
    return CategoryProfile.from_validated(rows)


def signals(text):
    return [extract_field_signals("item_desc_eng", text)]


def test_category_is_liquid_mixed_or_weight_from_the_workbooks_own_rows():
    built = profile()
    assert built.view(MILK).liquid and not built.view(MILK).mixed
    assert built.view(SAUCE).mixed and not built.view(SAUCE).liquid
    assert not built.view(FLOUR).liquid and not built.view(FLOUR).mixed
    assert built.as_dict()["liquid_categories"] == ["Fresh Milk"]
    assert built.as_dict()["mixed_categories"] == ["Sauces"]


def test_a_thin_category_falls_back_to_a_wider_level_and_unknown_is_not_judged():
    built = profile()
    rare = {"section": "Oat Drinks", "subcategory": "Plant", "category": "Fresh Milk", "department": "Dairy"}
    view = built.view(rare)
    assert (view.level, view.name, view.liquid) == ("category", "Fresh Milk", True)
    unknown = built.view({"section": "X", "subcategory": "X", "category": "X", "department": "X"})
    assert (unknown.rows, unknown.liquid, unknown.mixed) == (0, False, False)
    assert built.plausible({"category": "X"}, "GM", Decimal(5)) is None


def test_a_row_never_vouches_for_itself():
    rows = [(MILK, "ML", Decimal(1000))] * 12 + [(MILK, "GM", Decimal(1000))] * 3
    built = CategoryProfile.from_validated(rows)
    assert built.view(MILK).liquid
    assert not built.view(MILK, leave_out="ML").liquid  # 11 of 14 is below 80%


def test_ounce_is_read_by_category():
    built = profile()
    assert guards.ounce_reading("OZ", built.view(MILK)) == "VOLUME"
    assert guards.ounce_reading("OZ", built.view(SAUCE)) == "REVIEW"
    assert guards.ounce_reading("OZ", built.view(FLOUR)) == "WEIGHT"
    assert guards.ounce_reading("KG", built.view(MILK)) == "WEIGHT"
    review = guards.fluid_ounce_review("9", Decimal(255), Decimal(266), "Chilli Sauce")
    assert review["severity"] == "REVIEW" and "255 GM" in review["message"] and "266 ML" in review["message"]
    assert review["code"] in guards.GUARD_REVIEW_CODES


def test_text_contradiction_needs_independent_text_that_does_not_fit():
    assert guards.text_contradiction(signals("XO SAUCE 255G"), Decimal(220), "GM", None)["code"] == "TEXT_CONTRADICTS_RESULT"
    assert guards.text_contradiction(signals("FLOUR 1KG"), Decimal(1000), "GM", None) is None
    assert guards.text_contradiction(signals("NOODLE 6 X 90GM"), Decimal(90), "GM", Decimal(6)) is None
    assert guards.text_contradiction(signals("PLAIN FLOUR"), Decimal(1000), "GM", None) is None
    assert guards.text_contradiction(signals("CUP 250ML"), Decimal(1), "EA", None) is None


def test_implausible_size_stops_a_text_reading_but_only_notes_a_conversion():
    built = profile()
    read = guards.implausible_size(built, FLOUR, Decimal(500000), "GM", read_from_text=True)
    converted = guards.implausible_size(built, FLOUR, Decimal(500000), "GM", read_from_text=False)
    assert (read["code"], read["severity"]) == ("SIZE_OUTSIDE_CATEGORY_RANGE", "REVIEW")
    assert (converted["code"], converted["severity"]) == ("SIZE_UNUSUAL_FOR_CATEGORY", "INFO")
    assert converted["code"] not in guards.GUARD_REVIEW_CODES
    assert guards.implausible_size(built, FLOUR, Decimal(1000), "GM", read_from_text=True) is None
    assert guards.implausible_size(built, FLOUR, Decimal(3), "GM", read_from_text=True) is not None
    assert guards.implausible_size(built, FLOUR, Decimal(3), "EA", read_from_text=True) is None


def test_whole_pack_total_is_raised_only_on_the_reliable_text_signal():
    issue = guards.legacy_is_pack_total(signals("PANCAKE 2'SX130GM"), Decimal(260), "GM", Decimal(2))
    assert issue["code"] == "LEGACY_MAY_BE_PACK_TOTAL" and issue["expected_value"] == "130 GM × 2"
    # The text states the legacy value too, or there is no text, or no pack: no signal.
    assert guards.legacy_is_pack_total(signals("PANCAKE 260GM (2X130GM)"), Decimal(260), "GM", Decimal(2)) is None
    assert guards.legacy_is_pack_total(signals("PANCAKE"), Decimal(260), "GM", Decimal(2)) is None
    assert guards.legacy_is_pack_total(signals("PANCAKE 130GM"), Decimal(260), "GM", Decimal(1)) is None


def test_a_count_in_a_measured_category_is_a_note_never_a_stop():
    built = profile()
    note = guards.count_in_weight_category("PC", "EA", built.view(FLOUR))
    assert (note["code"], note["severity"]) == ("COUNT_IN_MEASURED_CATEGORY", "INFO")
    assert note["code"] not in guards.GUARD_REVIEW_CODES
    assert guards.count_in_weight_category("KG", "GM", built.view(FLOUR)) is None
