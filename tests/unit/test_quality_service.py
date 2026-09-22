from dataclasses import fields

import pytest

from app.rules.registry import load_default_registry
from app.services import quality_service
from app.services.quality_service import (
    BlindInput,
    BusinessDecision,
    QualityService,
    load_business_decisions,
)


def service() -> QualityService:
    return QualityService(load_default_registry())


def item(item_no="1", *, group="A", legacy=("1", "KG"), klm=("1000", "GM", "1"),
         desc=None, policy="NO_CHANGE", findings=(), reason=None, pack_status=None):
    return {
        "item_no": item_no, "row_number": int(item_no) + 1, "group": group,
        "original": {
            "legacy_size": legacy[0], "legacy_uom": legacy[1],
            "standard_size": klm[0], "standard_uom": klm[1], "standard_pack_size": klm[2],
        },
        "context": {"item_desc_eng": desc},
        "findings": [{"code": code, "human_reason": f"{code} reason"} for code in findings],
        "reason_code": reason,
        "pack_result": {"status": pack_status} if pack_status else None,
        "application_policy": policy,
        "review": {"overall_status": "NOT_REQUIRED"},
    }


def capability(report, key):
    return next(row for row in report["capabilities"] if row["key"] == key)


def decision(report, decision_id):
    return next(row for row in report["decisions"] if row["id"] == decision_id)


def test_blind_input_cannot_carry_the_answer_key():
    names = {field.name for field in fields(BlindInput)}
    assert not {name for name in names if name.startswith("standard")}
    assert names == {
        "legacy_size", "legacy_uom", "item_desc_eng", "item_desc_local",
        "web_description_eng", "web_description_chi",
    }


def test_predictions_do_not_change_when_the_hidden_answers_change():
    first = item(klm=("1000", "GM", "4"), desc="JUICE 4 x 200ML")
    second = item(klm=("999999", "EA", "77"), desc="JUICE 4 x 200ML")
    quality = service()
    for predict in (quality.predict_size_and_unit, quality.predict_pack_size):
        assert predict(BlindInput.from_item(first)) == predict(BlindInput.from_item(second))


def test_matches_count_as_agreement_and_disagreements_wait_for_a_decision():
    report = service().build_report({"original_file_name": "book.xlsx"}, [
        item("1"),                                                   # 1 KG -> 1000 GM: agrees
        item("2", legacy=("17.1", "OZ"), klm=("484", "GM", "1")),    # 485 vs 484: rounding, agrees
        item("3", legacy=("12.3", "OZ"), klm=("375", "GM", "1")),    # 349 vs 375: disagrees
        item("4", legacy=("10", "PC"), klm=("200", "GM", "1")),      # EA vs GM: different kind
        item("5", legacy=("4.5", "GM"), klm=("4.5", "GM", "1")),     # identity keeps decimals
        item("6", legacy=("16", "ST"), klm=("473", "ML", "1")),      # no rule: not tested
        item("7", group="B", legacy=("1", "KG"), klm=("1", "KG", None)),  # not an answer key
    ])

    unit = capability(report, "unit_conversion")
    assert (unit["tested"], unit["agreed"], unit["awaiting_decision"]) == (5, 3, 2)
    # Matching Excel is 3 of 5, but nothing shows the agent was wrong on the other two, so
    # they are left out of the score rather than counted against it.
    assert (unit["match_percent"], unit["scored"], unit["accuracy_percent"]) == (60.0, 3, 100.0)
    assert unit["state"] == "AWAITING_DECISION"
    assert (unit["confirmed_correct"], unit["confirmed_incorrect"]) == (0, 0)
    assert decision(report, "legacy_size_disagrees")["products_affected"] == 1
    assert decision(report, "legacy_unit_different_kind")["products_affected"] == 1
    assert decision(report, "legacy_size_disagrees")["example"].startswith("3: 12.3 OZ works out to 349 GM")
    # Disagreements are listed first so a stakeholder sees the open questions.
    assert [example["agrees"] for example in unit["examples"]][:2] == [False, False]


def test_pack_size_is_read_from_text_only_and_compared_with_excel():
    report = service().build_report({}, [
        item("1", klm=("200", "ML", "4"), desc="JUICE 4 x 200ML"),
        item("2", klm=("227", "GM", "36"), desc="DACE CASE 12 X 227GM"),
        item("3", klm=("500", "ML", "1"), desc="GREEN TEA"),  # no pack in text: not tested
    ])
    pack = capability(report, "pack_size")
    assert (pack["tested"], pack["agreed"], pack["awaiting_decision"]) == (2, 1, 1)
    assert decision(report, "pack_size_disagrees")["products_affected"] == 1


def test_a_recorded_decision_scores_the_waiting_products(monkeypatch):
    version, decisions = load_business_decisions()
    decided = tuple(
        row.model_copy(update={"status": "DECIDED", "resolution": "AGENT_CORRECT"})
        if row.id == "legacy_size_disagrees" else row
        for row in decisions
    )
    monkeypatch.setattr(quality_service, "load_business_decisions", lambda: (version, decided))

    report = service().build_report({}, [
        item("1"), item("3", legacy=("12.3", "OZ"), klm=("375", "GM", "1")),
    ])
    unit = capability(report, "unit_conversion")
    assert (unit["agreed"], unit["awaiting_decision"], unit["confirmed_correct"]) == (1, 0, 1)
    assert unit["state"] == "MEASURED"


def test_ai_reading_is_reported_as_not_measured_with_the_available_sample():
    report = service().build_report({}, [
        item("1", desc="JUICE 500ML", klm=("500", "ML", "1"), legacy=("500", "ML")),
        item("2", desc="GREEN TEA"),
    ])
    ai = capability(report, "ai_description_reading")
    assert (ai["state"], ai["accuracy_percent"], ai["available_to_test"]) == ("NOT_MEASURED", None, 1)
    assert decision(report, "approve_ai_reading_test")["products_affected"] == 1


def test_workload_is_plain_language_and_shares_exclude_purged_rows():
    report = service().build_report({}, [
        item("1"), item("2", policy="OBSERVATION_ONLY"),
        item("3", group="B", policy="AUTO_APPLY"),
        item("4", policy="REVIEW_REQUIRED", findings=["PACKAGING_HIERARCHY_AMBIGUOUS"]),
        item("5", group="C", policy="UNRESOLVED", legacy=(None, None), klm=(None, None, None)),
        item("6", group="SKIPPED_PURGED"),
    ])
    workload = {row["key"]: (row["products"], row["share_percent"]) for row in report["workload"]}
    assert workload == {
        "ALREADY_CORRECT": (2, 40.0), "AUTO_APPLY": (1, 20.0), "REVIEW_REQUIRED": (1, 20.0),
        "UNRESOLVED": (1, 20.0), "SKIPPED": (1, None),
    }
    assert decision(report, "case_pack_meaning")["products_affected"] == 1
    assert decision(report, "left_blank_is_correct")["products_affected"] == 1


def test_decisions_file_is_valid_and_written_for_stakeholders():
    _, decisions = load_business_decisions()
    assert len({row.id for row in decisions}) == len(decisions)
    for row in decisions:
        assert isinstance(row, BusinessDecision) and len(row.options) >= 2
        # No internal vocabulary may reach a client.
        text = " ".join([row.title, row.why_it_matters, row.unlocks, *row.options])
        for jargon in ("K/L/M", "Group A", "Group B", "Group C", "ADK", "deterministic", "regex", "ruleset"):
            assert jargon not in text, (row.id, jargon)


def test_one_overall_accuracy_figure_covers_every_blind_check(monkeypatch):
    items = [
        item("1", desc="JUICE 4 x 200ML", klm=("1000", "GM", "4")),          # unit ok, pack ok
        item("2", desc="DACE 12 X 227GM", klm=("1000", "GM", "36")),          # unit ok, pack differs
        item("3", legacy=("12.3", "OZ"), klm=("375", "GM", "1")),             # unit differs, no witness
    ]
    accuracy = service().build_report({}, items)["accuracy"]
    assert (accuracy["checks"], accuracy["scored"], accuracy["correct"], accuracy["percent"]) == (5, 3, 3, 100.0)
    assert (accuracy["awaiting_decision"], accuracy["misses"], accuracy["match_percent"]) == (2, 0, 60.0)
    assert sum(row["products"] for row in accuracy["breakdown"]) == accuracy["checks"]

    version, decisions = load_business_decisions()
    decided = tuple(
        row.model_copy(update={"status": "DECIDED", "resolution": "AGENT_CORRECT"})
        if row.id == "legacy_size_disagrees" else
        row.model_copy(update={"status": "DECIDED", "resolution": "EXCEL_CORRECT"})
        if row.id == "pack_size_disagrees" else row
        for row in decisions
    )
    monkeypatch.setattr(quality_service, "load_business_decisions", lambda: (version, decided))
    accuracy = service().build_report({}, items)["accuracy"]
    # One decided for the agent, one against: both are now scored.
    assert (accuracy["scored"], accuracy["correct"], accuracy["misses"], accuracy["percent"]) == (5, 4, 1, 80.0)
    assert accuracy["awaiting_decision"] == 0


def test_the_description_is_the_witness_between_legacy_and_excel():
    data_problem = item("1", legacy=("3", "OZ"), klm=("170", "GM", "1"), desc="GARLIC SALT 85G")
    caught = item("2", legacy=("220", "GM"), klm=("255", "GM", "1"), desc="XO SAUCE 255G")
    report = service().build_report({}, [data_problem, caught, item("3")])
    unit = capability(report, "unit_conversion")
    verdicts = {row["verdict"]: row["products"] for row in unit["breakdown"]}
    # 1: text backs the agent (85 GM), Excel says 170 -> a problem in the data, not scored.
    # 2: text backs Excel; production would stop the legacy value -> a correct catch.
    assert verdicts == {"CORRECT": 1, "CORRECT_CATCH": 1, "DATA_PROBLEM": 1}
    assert (unit["scored"], unit["correct"], unit["data_problems"], unit["accuracy_percent"]) == (2, 2, 1, 100.0)
    assert report["accuracy"]["bad_values_stopped"] == 1


def test_a_whole_pack_total_is_an_honest_miss():
    report = service().build_report({}, [item("1", legacy=("350", "GM"), klm=("70", "GM", "5")), item("2")])
    unit = capability(report, "unit_conversion")
    assert (unit["misses"], unit["scored"], unit["accuracy_percent"]) == (1, 2, 50.0)


def produced(item_no, *, group="B", before=("1", "KG"), legacy=("1", "KG"), result=("1000", "GM"),
             desc=None, policy="AUTO_APPLY", rule="KG_TO_GM", verification=None, review="NOT_REQUIRED"):
    row = item(item_no, group=group, legacy=legacy, klm=(before[0], before[1], "1"), desc=desc, policy=policy)
    row["field_proposals"] = {"standard_size": result[0], "standard_uom": result[1], "standard_pack_size": None}
    row["rule"] = {"rule_id": rule}
    row["verification"] = verification
    row["review"] = {"overall_status": review}
    return row


def result_row(report, key):
    return next(row for row in report["result_accuracy"]["rows"] if row["key"] == key)


def test_results_are_checked_against_independent_product_text():
    report = service().build_report({}, [
        produced("1", desc="FLOUR 1KG"),                                   # text confirms 1000 GM
        produced("2", desc="FLOUR 6 X 500G", result=("3000", "GM")),       # whole-pack total confirms
        produced("3", before=(None, None), legacy=("220", "GM"), result=("220", "GM"), desc="XO SAUCE 255G"),
        produced("4", desc="PLAIN FLOUR"),                                 # no size in text: not checked
        produced("5", before=("1", "PC"), legacy=("1", "PC"), result=("1", "EA"), desc="CUP 250ML"),  # a count
    ])
    converted, filled = result_row(report, "CONVERTED"), result_row(report, "FILLED")
    assert (converted["results"], converted["text_checked"], converted["text_confirmed"]) == (4, 2, 2)
    assert (filled["results"], filled["text_checked"], filled["text_contradicted"]) == (1, 1, 1)
    assert filled["examples"][0]["note"] == "The description states “255G”, which does not fit 220 GM."
    headline = report["result_accuracy"]["headline"]
    assert (headline["basis"], headline["results"], headline["checked"], headline["correct"], headline["percent"]) == (
        "PRODUCT_TEXT", 5, 3, 2, 66.7,
    )


def test_results_read_from_the_text_cannot_be_confirmed_by_that_text():
    pack = produced("1", desc="JUICE 4 x 200ML", result=(None, None))
    pack["field_proposals"] = {"standard_size": None, "standard_uom": None, "standard_pack_size": "4"}
    row = result_row(service().build_report({}, [pack]), "PACK")
    assert (row["text_checkable"], row["text_checked"], row["accuracy_percent"], row["basis"]) == (False, 0, None, None)


def test_reviewer_verdicts_take_over_the_headline_once_there_are_enough():
    items = [
        produced(str(n), desc="FLOUR 1KG", verification={"verdict": "CORRECT" if n > 3 else "WRONG"})
        for n in range(1, 31)
    ]
    headline = service().build_report({}, items)["result_accuracy"]["headline"]
    assert (headline["basis"], headline["checked"], headline["correct"], headline["percent"]) == (
        "REVIEWER", 30, 27, 90.0,
    )
    # One fewer verdict and the thin reviewer figure does not replace the text check.
    items[0]["verification"] = None
    assert service().build_report({}, items)["result_accuracy"]["headline"]["basis"] == "PRODUCT_TEXT"


def test_acting_on_a_suggestion_counts_as_a_verdict():
    rows = [
        produced("1", group="A", policy="REVIEW_REQUIRED", rule="LEGACY_COMPARISON", review="APPROVED"),
        produced("2", group="A", policy="REVIEW_REQUIRED", rule="LEGACY_COMPARISON", review="OVERRIDDEN"),
        produced("3", group="A", policy="REVIEW_REQUIRED", rule="LEGACY_COMPARISON", review="PENDING"),
    ]
    suggested = result_row(service().build_report({}, rows), "SUGGESTED_SIZE")
    assert (suggested["results"], suggested["verified"], suggested["verified_correct"]) == (3, 2, 1)


def test_verification_sample_is_repeatable_and_shows_existing_verdicts():
    items = [produced(str(n), verification={"verdict": "WRONG"} if n == 7 else None) for n in range(1, 41)]
    first = QualityService.verification_sample("job-1", items, "CONVERTED", 10)
    again = QualityService.verification_sample("job-1", list(reversed(items)), "CONVERTED", 10)
    other = QualityService.verification_sample("job-2", items, "CONVERTED", 10)

    order = [row["row_number"] for row in first["rows"]]
    assert order == [row["row_number"] for row in again["rows"]]
    assert order != [row["row_number"] for row in other["rows"]]
    assert order != sorted(order)  # not simply the first rows of the workbook
    assert (first["total_results"], len(first["rows"])) == (40, 10)
    assert first["rows"][0]["before"] == "1 KG × 1" and first["rows"][0]["result"] == "1000 GM × 1"
    full = QualityService.verification_sample("job-1", items, "CONVERTED", 40)
    assert [row["verdict"] for row in full["rows"] if row["item_no"] == "7"] == ["WRONG"]


def shelf(category, uom, count=20, size="1000"):
    rows = []
    for n in range(count):
        row = item(str(500 + len(category) * 100 + n), legacy=(size, uom), klm=(size, uom, "1"))
        row["context"]["category"] = category
        rows.append(row)
    return rows


def in_category(row, category):
    row["context"]["category"] = category
    return row


def test_blind_test_reads_ounces_the_way_production_does():
    milk = in_category(item("1", legacy=("48", "OZ"), klm=("1420", "ML", "1")), "Fresh Milk")
    sauce = in_category(item("2", legacy=("9", "OZ"), klm=("266", "ML", "1")), "Sauces")
    flour = in_category(item("3", legacy=("16", "OZ"), klm=("454", "GM", "1")), "Flour")
    mixed_shelf = shelf("Sauces", "ML", 10, "300") + [
        in_category(item(str(900 + n), legacy=("300", "GM"), klm=("300", "GM", "1")), "Sauces") for n in range(10)
    ]
    report = service().build_report({}, [
        milk, sauce, flour, *shelf("Fresh Milk", "ML"), *mixed_shelf, *shelf("Flour", "GM"),
    ])
    examples = {e["item_no"]: e for e in capability(report, "unit_conversion")["examples"]}
    assert "1" not in examples or examples["1"]["agrees"]       # fluid ounce: 48 OZ -> 1420 ML
    assert (examples["2"]["kind"], examples["2"]["agent"]) == ("SENT_TO_REVIEW", "A person decides")
    # Excel holds 266 ML, so the plain weight conversion would have been wrong: a correct catch.
    assert examples["2"]["verdict"] == "CORRECT_CATCH"
    assert capability(report, "unit_conversion")["no_answer"] == 1
    assert report["engine"]["liquid_categories"] == ["Fresh Milk"]
    assert report["engine"]["mixed_categories"] == ["Sauces"]


def test_whole_pack_totals_are_scored_by_the_recorded_decision():
    noodles = item("1", legacy=("350", "GM"), klm=("70", "GM", "5"))
    report = service().build_report({}, [noodles, item("2")])
    unit = capability(report, "unit_conversion")
    # D1 is recorded as decided: unit size is one piece, so this counts against the agent.
    assert (unit["agreed"], unit["awaiting_decision"], unit["confirmed_incorrect"]) == (1, 0, 1)
    decided = decision(report, "legacy_is_whole_pack")
    assert (decided["status"], decided["resolution"], decided["products_affected"]) == ("DECIDED", "EXCEL_CORRECT", 1)
    assert "same total" in decided["example"]


def test_label_rounding_counts_as_the_same_answer():
    report = service().build_report({}, [item("1", legacy=("5", "LB"), klm=("2270", "GM", "1"))])
    assert capability(report, "unit_conversion")["agreed"] == 1


def test_safety_checks_and_engine_are_reported_in_plain_language():
    guarded = item("1", group="B", policy="REVIEW_REQUIRED")
    guarded["guards"] = [{"code": "OUNCE_MAY_BE_FLUID", "message": "9 OZ could be a weight (255 GM) or fluid ounces (266 ML)."}]
    noted = item("2", group="B", policy="AUTO_APPLY")
    noted["guards"] = [{"code": "COUNT_IN_MEASURED_CATEGORY", "message": "Only a count."}]
    job = {"ruleset_version": "poc-v3", "validation_policy": {"guards_version": "guards-v1"},
           "ai_reading_test": {"status": "COMPLETED", "prompt_version": "uom-inference-v2", "score": None}}
    report = service().build_report(job, [guarded, noted])

    assert [row["key"] for row in report["safety_checks"]] == ["OUNCE_MAY_BE_FLUID", "COUNT_IN_MEASURED_CATEGORY"]
    first = report["safety_checks"][0]
    assert (first["products"], first["effect"]) == (1, "Sent to a person with both values")
    assert first["example"].startswith("1 (") and "266 ML" in first["example"]
    engine = report["engine"]
    assert (engine["prompt_version"], engine["ruleset_version"], engine["processed_with_guards"]) == (
        "uom-inference-v3", "poc-v3", True,
    )
    assert engine["ai_test_is_current"] is False  # measured with v2 instructions
    old_job = service().build_report({}, [item("3")])
    assert old_job["engine"]["processed_with_guards"] is False and old_job["safety_checks"] == []
    for row in report["safety_checks"]:
        for jargon in ("guard", "K/L/M", "Group", "deterministic", "regex"):
            assert jargon not in row["check"] + row["effect"]
