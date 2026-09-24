from itertools import product

import pytest

from app.services.result_status import (
    GROUP_OF_STATUS,
    GROUPS,
    STATUSES,
    describe,
    differs,
    effective_status,
    group_query,
    how_label,
    outcome_group,
    status_query,
)


def values_at(document, path):
    """MongoDB-style dotted lookup; arrays fan out (``findings.code``)."""
    current = [document]
    for part in path.split("."):
        following = []
        for value in current:
            if isinstance(value, list):
                following.extend(v.get(part) for v in value if isinstance(v, dict) and part in v)
            elif isinstance(value, dict) and part in value:
                following.append(value[part])
        current = following
    return current


def matches(document, query):
    for key, condition in query.items():
        if key == "$and":
            ok = all(matches(document, part) for part in condition)
        elif key == "$or":
            ok = any(matches(document, part) for part in condition)
        elif key == "$nor":
            ok = not any(matches(document, part) for part in condition)
        else:
            found = values_at(document, key)
            if isinstance(condition, dict) and "$in" in condition:
                ok = any(value in condition["$in"] for value in found)
            elif isinstance(condition, dict) and "$nin" in condition:
                ok = not any(value in condition["$nin"] for value in found)
            elif condition is None:  # null matches both null and missing
                ok = not found or None in found
            else:
                ok = condition in found
        if not ok:
            return False
    return True


def documents():
    routes = ("A", "B", "C", "INCOMPLETE", "VALIDATION_REVIEW", "SKIPPED_PURGED", "DATA_SHAPE_ERROR")
    policies = (None, "NO_CHANGE", "AUTO_APPLY", "OBSERVATION_ONLY", "REVIEW_REQUIRED", "UNRESOLVED")
    findings = ([], [{"code": "ROUNDING_ONLY_VARIANCE"}],
                [{"code": "ROUNDING_ONLY_VARIANCE"}, {"code": "LEGACY_UOM_MISMATCH"}],
                [{"code": "PARTLY_FILLED_ROW"}, {"code": "GAP_NOT_FOUND"}],
                [{"code": "DESCRIPTION_SIZE_DIFFERS"}])
    proposals = ({"standard_size": None, "standard_uom": None}, {"standard_size": "500", "standard_uom": "ML"}, None)
    reasons = (None, "RULE_CONVERSION", "NO_RULE")
    for route, policy, finding, proposal, reason in product(routes, policies, findings, proposals, reasons):
        document = {"route": route, "findings": finding}
        if reason:
            document["reason_code"] = reason
        if policy:
            document["application_policy"] = policy
        if proposal is not None:
            document["field_proposals"] = proposal
        yield document


def test_every_row_has_exactly_one_status_and_the_filter_agrees_with_it():
    for document in documents():
        selected = [status for status in STATUSES if matches(document, status_query(status))]
        assert selected == [effective_status(document)], document


def test_every_row_has_exactly_one_group_and_the_filter_agrees_with_it():
    for document in documents():
        selected = [group for group in GROUPS if matches(document, group_query(group))]
        assert selected == [outcome_group(document)], document


def test_the_group_is_the_outcome_not_the_method():
    assert GROUP_OF_STATUS == {
        "NO_CHANGE": "A", "OBSERVATION_ONLY": "A",
        "AUTO_APPLY": "B",
        "REVIEW_REQUIRED": "C", "UNRESOLVED": "C", "INVALID": "C",
        "SKIPPED": "PURGED",
    }
    # A row the tool checked (route A) and changed is Group B.
    assert outcome_group({"route": "A", "application_policy": "AUTO_APPLY"}) == "B"
    # A converted row (route B) whose conversion was raised is Group C.
    assert outcome_group({"route": "B", "application_policy": "REVIEW_REQUIRED",
                          "field_proposals": {"standard_size": "454", "standard_uom": "GM"}}) == "C"
    # A converted row with nothing to change is Group A.
    assert outcome_group({"route": "B", "application_policy": "NO_CHANGE",
                          "field_proposals": {"standard_size": "454", "standard_uom": "GM"}}) == "A"
    # A description that states a different size is raised, even though nothing changed.
    assert outcome_group({"route": "A", "application_policy": "NO_CHANGE",
                          "findings": [{"code": "DESCRIPTION_SIZE_DIFFERS"}]}) == "C"
    # Values that cannot be used are raised.
    assert outcome_group({"route": "DATA_SHAPE_ERROR"}) == "C"
    assert outcome_group({"route": "SKIPPED_PURGED"}) == "PURGED"


def test_a_persons_decision_does_not_move_the_row():
    raised = {"route": "A", "application_policy": "REVIEW_REQUIRED"}
    for decision in ("APPROVED", "REJECTED", "OVERRIDDEN"):
        assert outcome_group({**raised, "review": {"overall_status": decision}}) == "C"


def test_rows_stored_before_the_rename_read_their_route_from_group():
    assert outcome_group({"group": "SKIPPED_PURGED"}) == "PURGED"
    assert describe({"group": "C", "field_proposals": {"standard_size": None, "standard_uom": None}}) == {
        "group": "C", "route": "C", "route_label": "Read from the description", "status": "UNRESOLVED",
        "status_label": "Could not determine",
        "how": "Read from the description",
        "field_proposals": {"standard_size": None, "standard_uom": None},
    }


def test_how_names_where_each_change_came_from():
    filled = {
        "route": "INCOMPLETE", "application_policy": "AUTO_APPLY",
        "original": {"standard_size": None, "standard_uom": "GM", "standard_pack_size": None},
        "field_proposals": {"standard_size": "500", "standard_uom": None, "standard_pack_size": "1"},
        "field_provenance": {"standard_size": {"method": "RULE", "rule_id": "GAP_FROM_LEGACY"},
                             "standard_pack_size": {"method": "RULE", "rule_id": "SINGLE_ITEM_DEFAULT"}},
    }
    assert how_label(filled) == "Old size, unit table + Pack rule: single item"
    # A proposal equal to what Excel already has is not a change.
    same_pack = {**filled, "original": {**filled["original"], "standard_pack_size": 1}}
    assert how_label(same_pack) == "Old size, unit table"
    read = {"route": "C", "application_policy": "AUTO_APPLY", "original": {},
            "field_proposals": {"standard_size": "6", "standard_uom": "EA", "standard_pack_size": None},
            "field_provenance": {"standard_size": {"method": "AI_INFERENCE"}, "standard_uom": {"method": "AI_INFERENCE"}}}
    assert how_label(read) == "Read from the description"
    converted = {"route": "B", "application_policy": "AUTO_APPLY",
                 "original": {"standard_size": None, "legacy_size": "1", "legacy_uom": "KG"},
                 "field_proposals": {"standard_size": "1000", "standard_uom": "GM", "standard_pack_size": None},
                 "field_provenance": {"standard_size": {"method": "RULE", "rule_id": "KG_TO_GM"},
                                      "standard_uom": {"method": "RULE", "rule_id": "KG_TO_GM"}}}
    assert how_label(converted) == "Old size, unit table"
    in_place = {**converted, "original": {"standard_size": "1", "standard_uom": "KG", "standard_pack_size": "1"}}
    assert how_label(in_place) == "Unit table"
    assert how_label({"route": "A", "application_policy": "NO_CHANGE"}) == "Checked existing values"
    assert how_label({"route": "SKIPPED_PURGED"}) == "Skipped, purged"


def test_differs_compares_numbers_as_numbers_and_text_exactly():
    assert not differs("1", 1)
    assert not differs("500.0", "500")
    assert not differs("GM", " GM")
    assert differs("ML", "ml")
    assert differs("1000", "1")
    assert differs("ML", None)


def test_rows_stored_by_an_older_ledger_are_still_shown_correctly():
    # The earlier ledger stored these as NO_CHANGE.
    blank_c = {"route": "C", "application_policy": "NO_CHANGE",
               "field_proposals": {"standard_size": None, "standard_uom": None, "standard_pack_size": None}}
    purged = {"route": "SKIPPED_PURGED", "application_policy": "NO_CHANGE"}
    assert effective_status(blank_c) == "UNRESOLVED"
    assert effective_status(purged) == "SKIPPED"
    assert effective_status({"route": "A", "application_policy": "NO_CHANGE"}) == "NO_CHANGE"
    # A pack-only proposal does not make an unknown size known.
    blank_c["field_proposals"]["standard_pack_size"] = "4"
    assert effective_status(blank_c) == "UNRESOLVED"


def test_unknown_status_is_rejected():
    with pytest.raises(ValueError):
        status_query("SOMETHING")


def test_a_legacy_unit_with_no_rule_is_could_not_determine_not_already_correct():
    voucher = {"route": "B", "reason_code": "NO_RULE", "application_policy": "NO_CHANGE",
               "field_proposals": {"standard_size": None, "standard_uom": None, "standard_pack_size": None}}
    assert effective_status(voucher) == "UNRESOLVED"
    converted = {**voucher, "reason_code": "RULE_CONVERSION",
                 "field_proposals": {"standard_size": "454", "standard_uom": "GM", "standard_pack_size": None},
                 "application_policy": "AUTO_APPLY"}
    assert effective_status(converted) == "AUTO_APPLY"
