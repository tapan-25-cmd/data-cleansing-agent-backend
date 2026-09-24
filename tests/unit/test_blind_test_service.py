from app.rules.registry import load_default_registry
from app.services.blind_test_service import MUTATIONS, conversion_test, seeded_error_test, silent_candidates
from app.services.group_a_validator import GroupAValidator
from app.services.rule_engine import RuleEngine


def row(n, legacy_size, legacy_uom, size, uom, pack=1, text="FILLER"):
    return {"row_number": n, "item_no": f"{n:06d}", "route": "A", "department": "03_Grocery 2",
            "context": {"item_desc_eng": text, "category": "Noodles"},
            "original": {"legacy_size": legacy_size, "legacy_uom": legacy_uom, "standard_size": size, "standard_uom": uom, "standard_pack_size": pack},
            "field_proposals": {"standard_size": None, "standard_uom": None, "standard_pack_size": None},
            "application_policy": "NO_CHANGE", "reason_code": "VALIDATED_BASE_UNIT", "findings": []}


def test_conversion_test_compares_the_table_with_the_teams_values():
    items = [row(1, "1", "KG", 1000, "GM"), row(2, "12", "OZ", 375, "GM"), row(3, "350", "GM", 70, "GM", 5), row(4, "500", "ML", 500, "ML"), row(5, "6", "PC", 1, "EA", 6, "CUPS 6S")]
    report = conversion_test(items, RuleEngine(load_default_registry(), 0))
    s = report["summary"]
    assert s["needed_conversion"] == 3 and s["conversion_agree"] == 1 and s["conversion_differ"] == 1 and s["conversion_whole_pack"] == 1
    assert s["same_unit"] == 2 and s["same_unit_agree"] == 1 and s["same_unit_whole_pack"] == 1
    assert s["packs_over_one"] == 2 and s["packs_found_in_text"] == 1
    assert {r["item_no"] for r in report["rows"]} == {"000002", "000003", "000005"}  # only what did not simply agree, or a pack the text does not state


def test_seeded_errors_are_caught_by_the_checker():
    items = [row(1, "500", "ML", 500, "ML", text="JUICE 500ML"), row(2, "350", "GM", 70, "GM", 5, text="NOODLE")]
    report = seeded_error_test(items, GroupAValidator(RuleEngine(load_default_registry(), 0)), limit=None)
    s = report["summary"]
    assert s["products"] == 2 and s["untouched_flagged"] == 0
    by = {m["id"]: m for m in s["mutations"]}
    assert by["unit_swapped"]["CAUGHT"] == 2            # ML written as GM: the legacy is the other kind of unit
    assert by["size_is_total"]["tested"] == 1 and by["size_is_total"]["CAUGHT"] == 0 and by["size_is_total"]["NOTED_ONLY"] == 1
    assert by["decimal_shift"]["CAUGHT"] == 2
    assert by["pack_off_by_one"]["tested"] == 1
    assert {m["id"] for m in MUTATIONS} == set(by)


def test_silent_candidates_have_no_size_or_count_in_their_text():
    items = [row(1, "500", "ML", 500, "ML", text="JUICE 500ML"), row(2, "1", "PC", 1, "EA", text="GREEN TEA"), row(3, "1", "PC", 6, "EA", text="CANDLES 6PCS")]
    assert [x["item_no"] for x in silent_candidates(items)] == ["000002"]
