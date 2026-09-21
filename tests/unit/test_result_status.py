from itertools import product

import pytest

from app.services.result_status import STATUSES, effective_status, status_query


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
    groups = ("A", "B", "C", "VALIDATION_REVIEW", "SKIPPED_PURGED")
    policies = (None, "NO_CHANGE", "AUTO_APPLY", "OBSERVATION_ONLY", "REVIEW_REQUIRED", "UNRESOLVED")
    findings = ([], [{"code": "ROUNDING_ONLY_VARIANCE"}],
                [{"code": "ROUNDING_ONLY_VARIANCE"}, {"code": "LEGACY_UOM_MISMATCH"}])
    proposals = ({"standard_size": None, "standard_uom": None}, {"standard_size": "500", "standard_uom": "ML"}, None)
    for group, policy, finding, proposal in product(groups, policies, findings, proposals):
        document = {"group": group, "findings": finding}
        if policy:
            document["application_policy"] = policy
        if proposal is not None:
            document["field_proposals"] = proposal
        yield document


def test_every_row_has_exactly_one_status_and_the_filter_agrees_with_it():
    for document in documents():
        selected = [status for status in STATUSES if matches(document, status_query(status))]
        assert selected == [effective_status(document)], document


def test_rows_stored_by_an_older_ledger_are_still_shown_correctly():
    # The earlier ledger stored these as NO_CHANGE.
    blank_c = {"group": "C", "application_policy": "NO_CHANGE",
               "field_proposals": {"standard_size": None, "standard_uom": None, "standard_pack_size": None}}
    purged = {"group": "SKIPPED_PURGED", "application_policy": "NO_CHANGE"}
    assert effective_status(blank_c) == "UNRESOLVED"
    assert effective_status(purged) == "SKIPPED"
    assert effective_status({"group": "A", "application_policy": "NO_CHANGE"}) == "NO_CHANGE"
    # A pack-only proposal does not make an unknown size known.
    blank_c["field_proposals"]["standard_pack_size"] = "4"
    assert effective_status(blank_c) == "UNRESOLVED"


def test_unknown_status_is_rejected():
    with pytest.raises(ValueError):
        status_query("SOMETHING")
