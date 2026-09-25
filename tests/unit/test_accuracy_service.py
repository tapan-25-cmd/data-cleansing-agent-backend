"""Every live product lands in one named set of its outcome group; the text is the only
witness that confirms."""
from app.rules.registry import load_default_registry
from app.services.accuracy_service import SETS, AccuracyService, text_witness
from app.services.rule_engine import RuleEngine


def row(n, route, **changes):
    base = {"row_number": n, "item_no": f"{n:06d}", "route": route, "context": {"category": "Noodles"},
            "original": {"legacy_size": "500", "legacy_uom": "ML", "standard_size": 500, "standard_uom": "ML", "standard_pack_size": 1},
            "field_proposals": {"standard_size": None, "standard_uom": None, "standard_pack_size": None},
            "application_policy": "NO_CHANGE", "reason_code": "VALIDATED_BASE_UNIT", "findings": []}
    base.update(changes)
    return base


def service():
    return AccuracyService(RuleEngine(load_default_registry(), 0))


def sets_of(report, group):
    g = next(x for x in report["groups"] if x["group"] == group)
    return g, {s["id"]: s["products"] for s in g["sets"]}


def test_every_set_has_a_kind_a_name_and_a_reason():
    for sets in SETS.values():
        for s in sets:
            assert s["kind"] in {"CONFIRMED", "CONSISTENT", "FLAG", "ALARM", "WRONG", "UNVERIFIED"} and s["reason"] and s["name"]


def test_text_witness_supports_disputes_or_is_silent():
    from decimal import Decimal
    assert text_witness({"item_desc_eng": "JUICE 500ML"}, Decimal("500"), "ML", Decimal("1"))[0] == "SUPPORTS"
    assert text_witness({"web_description_chi": "蛋炒麵720克"}, Decimal("540"), "GM", Decimal("1"))[0] == "DISPUTES"
    assert text_witness({"item_desc_eng": "PAPER CUPS 12PCS"}, Decimal("1"), "EA", Decimal("12"))[0] == "SUPPORTS"
    assert text_witness({"item_desc_eng": "GREEN TEA"}, Decimal("500"), "ML", Decimal("1"))[0] == "SILENT"
    assert text_witness({"item_desc_eng": "VINEGAR 3G"}, Decimal("250"), "ML", Decimal("1"))[0] == "SILENT"  # other dimension


def test_group_a_is_judged_on_keeping_and_raised_rows_move_to_c():
    items = [
        row(1, "A"),                                                                                              # legacy copied
        row(2, "A", original={"legacy_size": "350", "legacy_uom": "GM", "standard_size": 70, "standard_uom": "GM", "standard_pack_size": 5}),  # whole pack
        row(3, "A", original={"legacy_size": "1", "legacy_uom": "PK", "standard_size": 6, "standard_uom": "EA", "standard_pack_size": 1},
            context={"item_desc_local_lang": "彩色長蠟燭6枝"}, application_policy="OBSERVATION_ONLY"),                 # text confirms
        row(4, "A", original={"legacy_size": "540", "legacy_uom": "GM", "standard_size": 540, "standard_uom": "GM", "standard_pack_size": 1},
            context={"web_description_chi": "蛋炒麵720克"}, application_policy="OBSERVATION_ONLY"),                     # kept, text disputes
        row(5, "A", original={"legacy_size": "107", "legacy_uom": "GM", "standard_size": 120, "standard_uom": "GM", "standard_pack_size": 1},
            application_policy="REVIEW_REQUIRED", findings=[{"code": "SIGNIFICANT_LEGACY_SIZE_MISMATCH"}], context={"item_desc_eng": "SATAY BEEF"}),  # raised: silent
        row(6, "A", original={"legacy_size": "1", "legacy_uom": "ST", "standard_size": 1, "standard_uom": "EA", "standard_pack_size": 1},
            application_policy="OBSERVATION_ONLY", findings=[{"code": "LEGACY_NOT_COMPARABLE"}], context={"item_desc_eng": "RICE PAPER"}),  # unverified
        row(7, "A", original={"legacy_size": "1", "legacy_uom": "PC", "standard_size": 2, "standard_uom": "EA", "standard_pack_size": 1},
            application_policy="REVIEW_REQUIRED", findings=[{"code": "SIGNIFICANT_LEGACY_SIZE_MISMATCH"}], context={"item_desc_eng": "SNOW BUN 2PCS"}),  # raised: text supports
        row(8, "A", original={"legacy_size": "540", "legacy_uom": "GM", "standard_size": 540, "standard_uom": "GM", "standard_pack_size": 1},
            context={"web_description_chi": "蛋炒麵720克"}, application_policy="REVIEW_REQUIRED", findings=[{"code": "DESCRIPTION_SIZE_DIFFERS"}]),  # raised: conflict
    ]
    report = service().build(items)
    g, sets = sets_of(report, "A")
    assert sets["a_legacy_exact"] == 1 and sets["a_legacy_pack"] == 1 and sets["a_text_confirms"] == 1
    assert sets["a_text_disputes"] == 1 and sets["a_unverified"] == 1
    assert g["products"] == 5 and g["scored"] == 4 and g["right"] == 3 and g["wrong"] == 1 and g["flags"] == 0
    assert g["accuracy_percent"] == 75.0 and g["confirmed"] == 1 and g["consistent"] == 2
    assert g["question"] == "Was keeping the values right?" and g["routes"] == [{"route": "Checked existing values", "products": 5}]
    c, raised = sets_of(report, "C")
    assert raised["c_flag_silent"] == 1 and raised["c_flag_text_supports"] == 1 and raised["c_flag_conflict"] == 1
    assert c["products"] == 3 and c["accuracy_percent"] == 100.0
    assert "same entry copied" in report["witness"]["1"] and "720克" in report["witness"]["4"]


def test_an_alarm_needs_quoted_evidence_that_states_excels_value():
    flagged = row(4, "A", original={"legacy_size": "12", "legacy_uom": "OZ", "standard_size": 355, "standard_uom": "ML", "standard_pack_size": 1},
                  application_policy="REVIEW_REQUIRED", findings=[{"code": "LEGACY_UOM_MISMATCH"}], context={"item_desc_eng": "WHITE VINEGAR 355ML"})
    name_only = [{"row_number": 4, "agreement": "AGREES_WITH_ENGINE", "ai": {"verdict": "EXCEL_RIGHT", "confidence": "HIGH", "needs_business_rule": False,
                  "evidence": [{"field": "item_desc_eng", "fragment": "WHITE VINEGAR"}]}}]
    value_quoted = [{"row_number": 4, "agreement": "AGREES_WITH_ENGINE", "ai": {"verdict": "EXCEL_RIGHT", "confidence": "HIGH", "needs_business_rule": False,
                     "evidence": [{"field": "item_desc_eng", "fragment": "355ML"}]}}]
    _, weak = sets_of(service().build([flagged], name_only), "C")
    strong_group, strong = sets_of(service().build([flagged], value_quoted), "C")
    assert weak["c_alarm"] == 0 and weak["c_flag_text_supports"] == 1
    assert strong["c_alarm"] == 1
    # A raise that was not needed counts against Group C.
    assert strong_group["scored"] == 1 and strong_group["right"] == 0 and strong_group["accuracy_percent"] == 0.0


def test_group_b_is_every_change_whatever_made_it():
    blank = {"standard_size": None, "standard_uom": None, "standard_pack_size": None}
    converted = row(10, "B", original={"legacy_size": "1", "legacy_uom": "KG", **blank}, field_proposals={"standard_size": "1000", "standard_uom": "GM", "standard_pack_size": None},
                    application_policy="AUTO_APPLY", reason_code="RULE_CONVERSION", context={"item_desc_eng": "FLOUR 1 KG", "category": "Flour"})
    contradicted = row(11, "B", original={"legacy_size": "220", "legacy_uom": "GM", **blank}, field_proposals={"standard_size": "220", "standard_uom": "GM", "standard_pack_size": None},
                       application_policy="AUTO_APPLY", reason_code="RULE_CONVERSION", context={"item_desc_eng": "XOS+OYS 255G", "category": "Sauces"})
    respelled = row(12, "B", original={"legacy_size": "200", "legacy_uom": "GM", "standard_size": 200, "standard_uom": "G", "standard_pack_size": 1},
                    field_proposals={"standard_size": None, "standard_uom": "GM", "standard_pack_size": None}, application_policy="AUTO_APPLY",
                    reason_code="STANDARD_FIELDS_NORMALIZATION", context={"item_desc_eng": "SAUCE", "category": "Sauces"})
    litre = row(13, "B", original={"legacy_size": "1", "legacy_uom": "LT", **blank}, field_proposals={"standard_size": "1000", "standard_uom": "ML", "standard_pack_size": None},
                application_policy="AUTO_APPLY", reason_code="RULE_CONVERSION", context={"item_desc_eng": "JUICE", "category": "Juices"})
    juices = [row(100 + i, "A", original={"legacy_size": "1", "legacy_uom": "LT", "standard_size": 1000, "standard_uom": "ML", "standard_pack_size": 1}, context={"category": "Juices"}) for i in range(25)]
    ounce = row(14, "B", original={"legacy_size": "32", "legacy_uom": "OZ", **blank}, field_proposals={"standard_size": "946", "standard_uom": "ML", "standard_pack_size": None},
                application_policy="AUTO_APPLY", reason_code="RULE_CONVERSION", context={"item_desc_eng": "KEFIR", "category": "Juices"})
    empty = {"legacy_size": None, "legacy_uom": None, **blank}
    read = row(15, "C", original=empty, context={"item_desc_eng": "JUICE 500ML"}, application_policy="AUTO_APPLY",
               field_proposals={"standard_size": "500", "standard_uom": "ML", "standard_pack_size": None})
    misread = row(16, "C", original=empty, context={"item_desc_eng": "JUICE 250ML"}, application_policy="AUTO_APPLY",
                  field_proposals={"standard_size": "500", "standard_uom": "ML", "standard_pack_size": None})
    unit_filled = row(17, "INCOMPLETE", original={"legacy_size": "500", "legacy_uom": "G", "standard_size": 500, "standard_uom": None, "standard_pack_size": 1},
                      application_policy="AUTO_APPLY", field_proposals={"standard_size": None, "standard_uom": "GM", "standard_pack_size": None},
                      field_provenance={"standard_uom": {"method": "RULE", "rule_id": "GAP_FROM_LEGACY", "evidence_text": "old size 500 G = 500 GM"}},
                      context={"item_desc_eng": "SUGAR", "category": "Sugar"})
    g, sets = sets_of(service().build([converted, contradicted, respelled, litre, ounce, read, misread, unit_filled, *juices]), "B")
    assert sets["b_text_confirms"] == 1 and sets["b_text_disagrees"] == 1 and sets["b_unit_respelled"] == 1
    assert sets["b_unverified"] == 1, "a litre conversion in a millilitre category proves nothing"
    assert sets["b_ounce_by_category"] == 1, "only an ounce is settled by the category's unit"
    assert sets["b_read_confirmed"] == 1 and sets["b_read_wrong"] == 1 and sets["b_gap_matches"] == 1
    assert g["products"] == 8 and g["scored"] == 7 and g["right"] == 5 and g["wrong"] == 2
    assert {r["route"] for r in g["routes"]} == {"Converted from the old size", "Read from the description", "Half-filled row"}


def test_group_c_is_judged_on_whether_raising_was_right():
    blank = {"legacy_size": None, "legacy_uom": None, "standard_size": None, "standard_uom": None, "standard_pack_size": None}
    silent = row(7, "C", original=blank, context={"item_desc_eng": "GREEN TEA", "item_desc_local_lang": "綠茶"}, application_policy="UNRESOLVED", reason_code="NOT_IN_DESCRIPTION")
    unread = row(8, "C", original=blank, context={"item_desc_eng": "BLACK TEA"}, application_policy="UNRESOLVED", reason_code="NOT_IN_DESCRIPTION")
    written = row(9, "C", original=blank, context={"item_desc_eng": "GREEN TEA 500ML"}, application_policy="UNRESOLVED", reason_code="NOT_IN_DESCRIPTION")
    pack = row(12, "C", original=blank, context={"item_desc_eng": "SMALL CAN BEER 4'S"}, application_policy="UNRESOLVED", reason_code="NOT_IN_DESCRIPTION",
               field_proposals={"standard_size": None, "standard_uom": None, "standard_pack_size": "4"}, findings=[{"code": "AI_PACK_NEEDS_CONFIRMATION"}])
    gap = row(13, "INCOMPLETE", original={**blank, "standard_uom": "GM"}, context={"item_desc_eng": "COOKIES"}, application_policy="REVIEW_REQUIRED",
              findings=[{"code": "PARTLY_FILLED_ROW"}, {"code": "GAP_NOT_FOUND", "human_reason": "The size is missing and could not be found."}])
    unusable = row(14, "DATA_SHAPE_ERROR", original={**blank, "standard_size": "abc"}, application_policy="NO_CHANGE")
    no_unit = row(15, "B", original={**blank, "legacy_size": "1", "legacy_uom": "ST"}, application_policy="UNRESOLVED", reason_code="NO_RULE")
    reasoning = [{"row_number": 7, "agreement": "CANNOT_TELL", "ai": {"verdict": "CANNOT_TELL", "confidence": "LOW", "evidence": []}}]
    report = service().build([silent, unread, written, pack, gap, unusable, no_unit], reasoning)
    g, sets = sets_of(report, "C")
    assert sets["c_nothing_written"] == 2 and sets["c_missed"] == 1 and sets["c_flag_pack"] == 1
    assert sets["c_gap_review"] == 1 and sets["c_unusable"] == 1 and sets["c_blank_unit"] == 1
    assert g["products"] == 7 and g["scored"] == 7 and g["right"] == 6 and g["wrong"] == 1 and g["coverage_percent"] == 100.0
    assert report["witness"]["13"] == "The size is missing and could not be found."


def test_every_live_product_lands_in_exactly_one_set():
    items = [row(1, "A"), row(2, "SKIPPED_PURGED"), row(3, "A", application_policy="REVIEW_REQUIRED", findings=[{"code": "SOMETHING_NEW"}])]
    report = service().build(items)
    # Our sets partition the products; the client's measure lists (m_*) cut across them.
    placed = sorted(r for key, rows in report["membership"].items() if not key.startswith("m_") for r in rows)
    assert placed == [1, 3]
    assert report["membership"]["c_other"] == [3]
