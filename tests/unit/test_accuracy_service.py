"""Every live product lands in one named set; the text is the only witness that confirms."""
from app.rules.registry import load_default_registry
from app.services.accuracy_service import SETS, AccuracyService, text_witness
from app.services.rule_engine import RuleEngine


def row(n, group, **changes):
    base = {"row_number": n, "item_no": f"{n:06d}", "group": group, "context": {"category": "Noodles"},
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


def test_group_a_text_beats_legacy_and_flags_stay_out_of_the_score():
    items = [
        row(1, "A"),                                                                                              # legacy copied
        row(2, "A", original={"legacy_size": "350", "legacy_uom": "GM", "standard_size": 70, "standard_uom": "GM", "standard_pack_size": 5}),  # whole pack
        row(3, "A", original={"legacy_size": "1", "legacy_uom": "PK", "standard_size": 6, "standard_uom": "EA", "standard_pack_size": 1},
            context={"item_desc_local_lang": "彩色長蠟燭6枝"}, application_policy="OBSERVATION_ONLY"),                 # text confirms
        row(4, "A", original={"legacy_size": "540", "legacy_uom": "GM", "standard_size": 540, "standard_uom": "GM", "standard_pack_size": 1},
            context={"web_description_chi": "蛋炒麵720克"}, application_policy="OBSERVATION_ONLY", findings=[{"code": "DESCRIPTION_MEASUREMENT_MISMATCH"}]),  # text disputes
        row(5, "A", original={"legacy_size": "107", "legacy_uom": "GM", "standard_size": 120, "standard_uom": "GM", "standard_pack_size": 1},
            application_policy="REVIEW_REQUIRED", findings=[{"code": "SIGNIFICANT_LEGACY_SIZE_MISMATCH"}], context={"item_desc_eng": "SATAY BEEF"}),  # silent flag
        row(6, "A", original={"legacy_size": "1", "legacy_uom": "ST", "standard_size": 1, "standard_uom": "EA", "standard_pack_size": 1},
            application_policy="OBSERVATION_ONLY", findings=[{"code": "LEGACY_NOT_COMPARABLE"}], context={"item_desc_eng": "RICE PAPER"}),  # unverified
        row(7, "A", original={"legacy_size": "1", "legacy_uom": "PC", "standard_size": 2, "standard_uom": "EA", "standard_pack_size": 1},
            application_policy="REVIEW_REQUIRED", findings=[{"code": "SIGNIFICANT_LEGACY_SIZE_MISMATCH"}], context={"item_desc_eng": "SNOW BUN 2PCS"}),  # legacy differs, text supports
    ]
    report = service().build(items)
    g, sets = sets_of(report, "A")
    assert sets["a_legacy_exact"] == 1 and sets["a_legacy_pack"] == 1 and sets["a_text_confirms"] == 1
    assert sets["a_text_disputes"] == 1 and sets["a_flag_silent"] == 1 and sets["a_unverified"] == 1 and sets["a_flag_text_supports"] == 1
    assert g["products"] == 7 and g["scored"] == 4 and g["right"] == 3 and g["wrong"] == 1 and g["flags"] == 2
    assert g["accuracy_percent"] == 75.0 and g["confirmed"] == 1 and g["consistent"] == 2
    assert "same entry copied" in report["witness"]["1"] and "720克" in report["witness"]["4"]


def test_an_alarm_needs_quoted_evidence_that_states_excels_value():
    flagged = row(4, "A", original={"legacy_size": "12", "legacy_uom": "OZ", "standard_size": 355, "standard_uom": "ML", "standard_pack_size": 1},
                  application_policy="REVIEW_REQUIRED", findings=[{"code": "LEGACY_UOM_MISMATCH"}], context={"item_desc_eng": "WHITE VINEGAR 355ML"})
    name_only = [{"row_number": 4, "agreement": "AGREES_WITH_ENGINE", "ai": {"verdict": "EXCEL_RIGHT", "confidence": "HIGH", "needs_business_rule": False,
                  "evidence": [{"field": "item_desc_eng", "fragment": "WHITE VINEGAR"}]}}]
    value_quoted = [{"row_number": 4, "agreement": "AGREES_WITH_ENGINE", "ai": {"verdict": "EXCEL_RIGHT", "confidence": "HIGH", "needs_business_rule": False,
                     "evidence": [{"field": "item_desc_eng", "fragment": "355ML"}]}}]
    _, weak = sets_of(service().build([flagged], name_only), "A")
    _, strong = sets_of(service().build([flagged], value_quoted), "A")
    assert weak["a_alarm"] == 0 and weak["a_flag_text_supports"] == 1
    assert strong["a_alarm"] == 1


def test_group_b_only_the_text_confirms_and_the_text_can_contradict():
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
    g, sets = sets_of(service().build([converted, contradicted, respelled, litre, ounce, *juices]), "B")
    assert sets["b_text_confirms"] == 1 and sets["b_text_disagrees"] == 1 and sets["b_unit_respelled"] == 1
    assert sets["b_unverified"] == 1, "a litre conversion in a millilitre category proves nothing"
    assert sets["b_ounce_by_category"] == 1, "only an ounce is settled by the category's unit"
    assert g["products"] == 5 and g["scored"] == 4 and g["right"] == 3 and g["accuracy_percent"] == 75.0 and g["coverage_percent"] == 80.0


def test_group_c_blank_is_a_win_only_with_a_second_reader_and_pack_flags_are_reachable():
    blank = {"legacy_size": None, "legacy_uom": None, "standard_size": None, "standard_uom": None, "standard_pack_size": None}
    silent = row(7, "C", original=blank, context={"item_desc_eng": "GREEN TEA", "item_desc_local_lang": "綠茶"}, application_policy="UNRESOLVED", reason_code="NOT_IN_DESCRIPTION")
    unread = row(8, "C", original=blank, context={"item_desc_eng": "BLACK TEA"}, application_policy="UNRESOLVED", reason_code="NOT_IN_DESCRIPTION")
    written = row(9, "C", original=blank, context={"item_desc_eng": "GREEN TEA 500ML"}, application_policy="UNRESOLVED", reason_code="NOT_IN_DESCRIPTION")
    read = row(10, "C", original=blank, context={"item_desc_eng": "JUICE 500ML"}, application_policy="AUTO_APPLY", field_proposals={"standard_size": "500", "standard_uom": "ML", "standard_pack_size": None})
    misread = row(11, "C", original=blank, context={"item_desc_eng": "JUICE 250ML"}, application_policy="AUTO_APPLY", field_proposals={"standard_size": "500", "standard_uom": "ML", "standard_pack_size": None})
    pack = row(12, "C", original=blank, context={"item_desc_eng": "SMALL CAN BEER 4'S"}, application_policy="UNRESOLVED", reason_code="NOT_IN_DESCRIPTION",
               field_proposals={"standard_size": None, "standard_uom": None, "standard_pack_size": "4"}, findings=[{"code": "AI_PACK_NEEDS_CONFIRMATION"}])
    reasoning = [{"row_number": 7, "agreement": "CANNOT_TELL", "ai": {"verdict": "CANNOT_TELL", "confidence": "LOW", "evidence": []}}]
    g, sets = sets_of(service().build([silent, unread, written, read, misread, pack], reasoning), "C")
    assert sets["c_nothing_right"] == 1 and sets["c_blank_unverified"] == 1 and sets["c_missed"] == 1
    assert sets["c_read"] == 1 and sets["c_wrong_read"] == 1 and sets["c_flag_pack"] == 1
    assert g["scored"] == 4 and g["right"] == 2 and g["accuracy_percent"] == 50.0
