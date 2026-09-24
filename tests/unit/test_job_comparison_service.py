"""Past run versus new run, row by row, plus the reviewer-confirmed answers."""
from app.services.job_comparison_service import (
    JobComparisonService,
    classify,
    describe,
    load_reviewer_cases,
    outcome,
    reviewer_verdict,
)


def row(**changes):
    item = {
        "row_number": 10, "item_no": "587501", "route": "A",
        "context": {"item_desc_eng": "DRIED NOODLE", "item_desc_local_lang": "優質光身麵"},
        "original": {
            "legacy_size": "350", "legacy_uom": "GM",
            "standard_size": 70, "standard_uom": "GM", "standard_pack_size": 5,
        },
        "field_proposals": {"standard_size": None, "standard_uom": None, "standard_pack_size": None},
        "application_policy": "NO_CHANGE", "reason_code": "VALIDATED_BASE_UNIT",
        "review": {"overall_status": "NOT_REQUIRED"}, "findings": [], "changes": [],
    }
    item.update(changes)
    return item


PAST_REVIEW = dict(
    field_proposals={"standard_size": "350", "standard_uom": "GM", "standard_pack_size": None},
    application_policy="REVIEW_REQUIRED", review={"overall_status": "PENDING"},
    findings=[{"code": "SIGNIFICANT_LEGACY_SIZE_MISMATCH", "severity": "REVIEW", "field": "standard_size",
               "evidence": [{"role": "CURRENT", "value": "70"}, {"role": "EXPECTED", "value": "350"}]}],
)
NEW_NOTE = dict(
    application_policy="OBSERVATION_ONLY",
    findings=[{"code": "LEGACY_TOTAL_CONSISTENT", "severity": "INFO", "field": "standard_size",
               "human_reason": "Legacy value agrees with the existing whole-pack total",
               "evidence": [{"role": "CURRENT", "value": "70 GM × 5"}, {"role": "EXPECTED", "value": "350 GM"}]}],
)


def test_a_review_that_became_a_note_is_reported_as_cleared_with_both_comments():
    report = JobComparisonService().build([row(**PAST_REVIEW)], [row(**NEW_NOTE)])

    assert report["summary"] == {
        "rows_compared": 1, "changed": 1, "missing_in_past": 0,
        "by_change": {"SAME": 0, "REVIEW_CLEARED": 1, "NOW_AUTOMATIC": 0, "VALUES_CHANGED": 0,
                      "SUGGESTION_CHANGED": 0, "NEW_REVIEW": 0, "NOW_UNRESOLVED": 0, "NOTE_CHANGED": 0},
        "past_status": {"REVIEW_REQUIRED": 1}, "new_status": {"OBSERVATION_ONLY": 1},
        # Raised in the past run, kept with a note now: the row moved from C to A.
        "past_group": {"C": 1}, "new_group": {"A": 1},
    }
    (line,) = report["rows"]
    assert (line["past_group"], line["group"], line["route"]) == ("C", "A", "A")
    assert line["change_label"] == "No longer needs review"
    assert line["past"]["status_label"] == "Needs your review"
    assert line["past"]["suggestion"]["total"] == "1750"          # what approving it would have done
    assert line["new"]["suggestion"] is None
    assert line["new"]["values"] == {"standard_size": "70", "standard_uom": "GM", "standard_pack_size": "5", "total": "350"}
    assert "whole pack: 70 GM × 5 = 350 GM" in line["new"]["comment"]
    assert "Check the pack" in line["past"]["comment"]
    assert line["explanation"].startswith("The past run asked a person to check this row")


def test_identical_rows_are_same_and_purged_rows_are_left_out():
    same = row()
    purged = row(row_number=11, route="SKIPPED_PURGED")
    report = JobComparisonService().build([same, purged], [same, purged])
    assert report["summary"]["rows_compared"] == 1
    assert report["rows"][0]["change"] == "SAME"


def test_final_values_follow_automatic_corrections_and_review_decisions():
    auto = outcome(row(route="B", application_policy="AUTO_APPLY", reason_code="RULE_CONVERSION",
                       original={"legacy_size": "1", "legacy_uom": "KG", "standard_size": None, "standard_uom": None, "standard_pack_size": None},
                       field_proposals={"standard_size": "1000", "standard_uom": "GM", "standard_pack_size": None},
                       changes=[{"field": "standard_size", "original": None, "proposed": "1000"}], method="RULE"))
    assert auto["values"]["standard_size"] == "1000" and auto["values"]["total"] is None
    overridden = outcome(row(**PAST_REVIEW, ) | {"review": {"overall_status": "OVERRIDDEN", "override_values": {"standard_size": "55", "standard_pack_size": "10"}}})
    assert overridden["values"] == {"standard_size": "55", "standard_uom": "GM", "standard_pack_size": "10", "total": "550"}


def test_change_classification_covers_every_direction():
    def out(status, values, suggestion=None):
        return {"status": status, "values": values, "suggestion": suggestion}
    seventy = {"standard_size": "70", "standard_uom": "GM", "standard_pack_size": "5"}
    other = {"standard_size": "350", "standard_uom": "GM", "standard_pack_size": "5"}
    assert classify(out("NO_CHANGE", seventy), out("NO_CHANGE", seventy)) == "SAME"
    assert classify(out("REVIEW_REQUIRED", seventy, other), out("OBSERVATION_ONLY", seventy)) == "REVIEW_CLEARED"
    assert classify(out("REVIEW_REQUIRED", seventy, other), out("AUTO_APPLY", other)) == "NOW_AUTOMATIC"
    assert classify(out("AUTO_APPLY", seventy), out("AUTO_APPLY", other)) == "VALUES_CHANGED"
    assert classify(out("REVIEW_REQUIRED", seventy, other), out("REVIEW_REQUIRED", seventy, seventy)) == "SUGGESTION_CHANGED"
    assert classify(out("NO_CHANGE", seventy), out("REVIEW_REQUIRED", seventy, other)) == "NEW_REVIEW"
    assert classify(out("AUTO_APPLY", seventy), out("UNRESOLVED", seventy)) == "NOW_UNRESOLVED"
    assert classify(out("NO_CHANGE", seventy), out("OBSERVATION_ONLY", seventy)) == "NOTE_CHANGED"


def test_reviewer_verdicts_distinguish_kept_suggested_and_contrary_answers():
    expected = {"standard_size": "70", "standard_uom": "GM", "standard_pack_size": "5"}
    kept = {"status": "NO_CHANGE", "values": expected, "suggestion": None}
    contrary = {"status": "REVIEW_REQUIRED", "values": expected,
                "suggestion": {"standard_size": "350", "standard_uom": "GM", "standard_pack_size": "5"}}
    suggested = {"status": "REVIEW_REQUIRED", "values": {"standard_size": "120", "standard_uom": "GM", "standard_pack_size": "5"},
                 "suggestion": expected}
    wrong = {"status": "NO_CHANGE", "values": {"standard_size": "120", "standard_uom": "GM", "standard_pack_size": "5"}, "suggestion": None}
    assert reviewer_verdict(expected, kept) == "ACHIEVED"
    assert reviewer_verdict(expected, {"status": "REVIEW_REQUIRED", "values": expected, "suggestion": None}) == "KEPT_UNDER_REVIEW"
    assert reviewer_verdict(expected, contrary) == "CONTRARY_PENDING"
    assert reviewer_verdict(expected, suggested) == "SUGGESTED"
    assert reviewer_verdict(expected, wrong) == "NOT_ACHIEVED"
    assert reviewer_verdict(expected, None) == "NOT_IN_RUN"


def test_reviewer_cases_are_scored_for_both_runs():
    version, reviewer, cases = load_reviewer_cases()
    assert version == "reviewer-confirmed-v1" and len(cases) == 10
    report = JobComparisonService().build([row(**PAST_REVIEW)], [row(**NEW_NOTE)])
    scored = {case["item_no"]: case for case in report["reviewer"]["cases"]}
    assert scored["587501"]["past"]["verdict"] == "CONTRARY_PENDING"
    assert scored["587501"]["new"]["verdict"] == "ACHIEVED"
    assert report["reviewer"]["new"]["ACHIEVED"] == 1
    assert report["reviewer"]["new"]["NOT_IN_RUN"] == 9
    assert report["rows"][0]["reviewer"]["new_verdict_label"] == "Matches the reviewer"


def test_values_are_described_the_way_a_person_writes_them():
    assert describe({"standard_size": "1000", "standard_uom": "GM", "standard_pack_size": "1"}) == "1000 GM × 1"
    assert describe({"standard_size": "1", "standard_uom": "EA", "standard_pack_size": None}) == "1 EA"
    assert describe({"standard_size": None, "standard_uom": None, "standard_pack_size": None}) == "blank"
