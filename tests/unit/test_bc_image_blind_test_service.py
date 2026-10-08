from app.agents.pack_reader import PackReading
from app.services.bc_image_blind_test_service import build, compare


def reading(size="90", uom="GM", count=5):
    return {"legible": True, "size": size, "uom": uom, "count": count, "printed": f"{count} x {size}{uom}"}


def expected(size="90", uom="GM", count=5):
    return {"standard_size": size, "standard_uom": uom, "standard_pack_size": count}


def test_exact_klm_match():
    assert compare(reading(), expected())["verdict"] == "EXACT_KLM_MATCH"


def test_equivalent_total_is_not_exact():
    result = compare(reading("90", "GM", 5), expected("450", "GM", 1))
    assert result["verdict"] == "EQUIVALENT_TOTAL_DIFFERENT_SPLIT"


def test_unit_conversion_is_normalized():
    assert compare(reading("1", "KG", 2), expected("1000", "GM", 2))["verdict"] == "EXACT_KLM_MATCH"


def test_nearest_whole_is_a_match():
    assert compare(reading("12.3", "OZ", 1), expected("349", "GM", 1))["verdict"] == "ROUNDING_EQUIVALENT_MATCH"


def test_pack_mismatch_and_unreadable_are_explicit():
    assert compare(reading(count=6), expected(count=5))["verdict"] == "PACK_SIZE_MISMATCH"
    assert compare({"legible": False, "note": "back photo blurred"}, expected())["verdict"] == "NO_READABLE_QUANTITY"


def test_nested_pack_counts_are_reconciled_to_consumption_units():
    reading = PackReading(
        legible=True, printed="200g x 4; x16 bags", size="200", uom="GM",
        inner_count=4, outer_count=16, confidence="HIGH",
    ).model_dump()

    assert reading["total_count"] == 64
    assert reading["count"] == 64
    assert compare(reading, expected("200", "GM", 64))["verdict"] == "EXACT_KLM_MATCH"


def test_build_uses_only_found_b_and_c_and_scores_c_proposal_separately():
    docs = [
        {"row_number": 2, "item_no": "B1", "group": "B", "status": "FOUND", "photo_reading": reading(),
         "excel": expected(), "final": expected(), "suggestion": None, "source": {"image_urls": ["one", "two"]}},
        {"row_number": 3, "item_no": "C1", "group": "C", "status": "FOUND", "photo_reading": reading(),
         "excel": None, "final": None, "suggestion": expected(), "source": {"image_urls": ["one"]}},
        {"row_number": 4, "item_no": "A1", "group": "A", "status": "FOUND", "photo_reading": reading(), "final": expected()},
        {"row_number": 5, "item_no": "B2", "group": "B", "status": "NOT_FOUND", "photo_reading": reading(), "final": expected()},
    ]
    report = build("job", docs)
    assert report["population"] == 2
    assert report["groups"]["B"]["exact"] == 1
    assert report["rows"][1]["final_comparison"]["verdict"] == "NOT_COMPARABLE"
    assert report["rows"][1]["proposal_comparison"]["verdict"] == "EXACT_KLM_MATCH"
    assert report["groups"]["C"]["exact"] == 1
