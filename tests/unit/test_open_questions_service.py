"""Every attention row lands in exactly one plain-language category, with its data."""
from app.rules.registry import load_default_registry
from app.services.open_questions_service import OpenQuestionsService, categorize, load_catalogue
from app.services.rule_engine import RuleEngine


def item(**changes):
    base = {
        "row_number": 17360, "item_no": "550624", "group": "A",
        "context": {"item_desc_eng": "TY SHRIMP CR NDL\\10", "item_desc_local_lang": "冬蔭蝦味奶油湯麵"},
        "original": {"legacy_size": "55", "legacy_uom": "GM", "standard_size": 550, "standard_uom": "GM", "standard_pack_size": 1},
        "field_proposals": {"standard_size": "55", "standard_uom": "GM", "standard_pack_size": "10"},
        "application_policy": "REVIEW_REQUIRED", "reason_code": "VALIDATED_BASE_UNIT",
        "review": {"overall_status": "PENDING"}, "changes": [],
        "findings": [{"code": "LINKED_SIZE_AND_PACK_SUGGESTION", "severity": "REVIEW", "field": "standard_size",
                      "evidence": [{"role": "CURRENT", "value": "550 GM × 1"}, {"role": "EXPECTED", "value": "\\10 (item description, English)"}],
                      "proposed": {"standard_size": "55", "standard_uom": "GM", "standard_pack_size": "10"}}],
    }
    base.update(changes)
    return base


def service():
    return OpenQuestionsService(RuleEngine(load_default_registry(), 0))


def test_catalogue_loads_and_every_category_has_a_group():
    catalogue = load_catalogue()
    groups = {g["id"] for g in catalogue["groups"]}
    assert catalogue["version"] == "open-questions-v1"
    assert all(c["group"] in groups for c in catalogue["categories"])


def test_first_matching_category_wins_and_correct_rows_are_left_out():
    assert categorize(item(), "REVIEW_REQUIRED") == "same_total_different_split"
    assert categorize(item(findings=[{"code": "SIGNIFICANT_LEGACY_SIZE_MISMATCH"}]), "REVIEW_REQUIRED") == "legacy_different_size"
    ea = item(original={"legacy_size": "10", "legacy_uom": "PC", "standard_size": 1, "standard_uom": "EA", "standard_pack_size": 6},
              findings=[{"code": "SIGNIFICANT_LEGACY_SIZE_MISMATCH"}])
    assert categorize(ea, "REVIEW_REQUIRED") == "legacy_counts_differently"
    assert categorize(item(findings=[{"code": "DESCRIPTION_MEASUREMENT_MISMATCH"}]), "OBSERVATION_ONLY") == "number_not_a_size"
    assert categorize(item(findings=[{"code": "DESCRIPTION_MEASUREMENT_MISMATCH"}]), "REVIEW_REQUIRED") == "description_size_differs"
    assert categorize(item(group="B", reason_code="RULE_CONVERSION", findings=[]), "AUTO_APPLY") == "converted_by_rule"
    assert categorize(item(findings=[]), "NO_CHANGE") is None
    assert categorize(item(findings=[]), "SKIPPED") is None
    assert categorize(item(findings=[{"code": "SOMETHING_NEW"}]), "REVIEW_REQUIRED") == "other_review"


def test_rows_carry_descriptions_legacy_excel_suggestion_and_options():
    report = service().build([item()], {})
    (row,) = report["rows"]
    assert row["category"] == "same_total_different_split"
    assert row["status_label"] == "Needs your review"
    assert row["legacy"] == {"text": "55 GM", "converted": "55 GM", "converted_size": "55", "converted_uom": "GM"}
    assert row["excel"]["total"] == "550" and row["suggestion"]["total"] == "550"
    assert [o["label"] for o in row["options"]] == ["Keep Excel as uploaded", "Use the suggestion"]
    assert row["options"][1]["text"] == "55 GM × 10"
    assert "55 GM × 10" in row["comment"]
    assert row["descriptions"][2] == {"field": "item_desc_eng", "label": "Item description (English)", "value": "TY SHRIMP CR NDL\\10"}
    category = next(c for g in report["groups"] for c in g["categories"] if c["id"] == "same_total_different_split")
    assert category["rows"] == 1 and category["statuses"] == {"REVIEW_REQUIRED": 1}
    assert report["rows_total"] == 1 and report["answered"] == 0


def test_answers_are_attached_to_their_category():
    report = service().build([item()], {"same_total_different_split": {"answer": "Use the split", "answered_by": "Eric"}})
    category = next(c for g in report["groups"] for c in g["categories"] if c["id"] == "same_total_different_split")
    assert category["answer"]["answer"] == "Use the split"
    assert report["answered"] == 1
