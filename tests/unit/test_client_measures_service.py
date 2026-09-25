"""The client's three Release 1 measures, computed the way their email sets them out."""
from app.rules.registry import load_default_registry
from app.services.accuracy_service import AccuracyService
from app.services.rule_engine import RuleEngine


def row(n, route, policy, original, proposals=None, provenance=None, findings=(), context=None, reason=None):
    return {"row_number": n, "item_no": f"{n:06d}", "route": route, "application_policy": policy,
            "original": original, "field_proposals": proposals or {"standard_size": None, "standard_uom": None, "standard_pack_size": None},
            "field_provenance": provenance or {}, "findings": [{"code": c} for c in findings],
            "context": context or {"category": "Snacks"}, "reason_code": reason}


def build(items, raised=()):
    return AccuracyService(RuleEngine(load_default_registry(), 0)).build(items, [], list(raised))


def converted(n, size, uom, proposed, target="GM"):
    return row(n, "B", "AUTO_APPLY", {"legacy_size": size, "legacy_uom": uom, "standard_size": None, "standard_uom": None, "standard_pack_size": None},
               {"standard_size": proposed, "standard_uom": target, "standard_pack_size": None},
               {"standard_size": {"method": "RULE", "rule_id": "X"}}, reason="RULE_CONVERSION")


def test_rule_accuracy_sorts_every_change_as_the_email_does():
    items = [
        converted(1, "1", "KG", "1000"),                        # exact
        converted(2, "16", "OZ", "454"),                        # 453.59: rounding, the rules agree? floor 453, nearest 454 -> differ
        converted(3, "10", "OZ", "283"),                        # 283.495: floor 283, nearest 283 -> agree
        converted(4, "12", "FZ", "355", target="ML"),           # fluid ounce
        converted(5, "1", "KG", "999"),                         # does not match
        row(6, "B", "AUTO_APPLY", {"legacy_size": "200", "legacy_uom": "GM", "standard_size": 200, "standard_uom": "G", "standard_pack_size": 1},
            {"standard_size": None, "standard_uom": "GM", "standard_pack_size": None}, reason="STANDARD_FIELDS_NORMALIZATION"),  # label only
    ]
    rule = build(items)["measures"]["rule_accuracy"]
    rows = {r["id"]: r["products"] for r in rule["rows"]}
    assert rows["m_rule_exact"] == 1 and rows["m_rule_round_differ"] == 1 and rows["m_rule_round_same"] == 1
    assert rows["m_rule_fluid"] == 1 and rows["m_rule_label"] == 1 and rows["m_rule_mismatch"] == 1
    assert (rule["tested"], rule["matched"], rule["percent"], rule["rules_differ"]) == (6, 5, 83.3, 1)


def test_already_correct_is_reported_as_consistency_with_the_legacy_size():
    same = {"legacy_size": "500", "legacy_uom": "GM", "standard_size": 500, "standard_uom": "GM", "standard_pack_size": 1}
    items = [
        row(1, "A", "NO_CHANGE", same),                                                             # identical, exact
        row(2, "A", "NO_CHANGE", {**same, "legacy_size": "1.1", "legacy_uom": "LB"}),                # 498.95 -> within one? no
        row(3, "A", "NO_CHANGE", {**same, "legacy_size": "16", "legacy_uom": "OZ", "standard_size": 454}),   # 453.59 within one
        row(4, "A", "NO_CHANGE", {**same, "legacy_size": "32", "legacy_uom": "OZ", "standard_size": 946, "standard_uom": "ML"}),  # fluid
        row(5, "A", "OBSERVATION_ONLY", same, findings=["ROUNDING_ONLY_VARIANCE"]),                  # with a note
    ]
    a = build(items)["measures"]["consistency"]
    assert (a["products"], a["already_correct"], a["with_note"]) == (5, 4, 1)
    assert (a["exact"], a["within_one"], a["fluid"], a["other"], a["identical"]) == (1, 1, 1, 1, 1)
    assert "Matches the legacy size" in a["relabel"]


def test_flag_precision_counts_justified_flags_and_the_comment_facts():
    blank = {"legacy_size": None, "legacy_uom": None, "standard_size": None, "standard_uom": None, "standard_pack_size": None}
    mismatch = row(1, "A", "REVIEW_REQUIRED", {"legacy_size": "107", "legacy_uom": "GM", "standard_size": 120, "standard_uom": "GM", "standard_pack_size": 1},
                   {"standard_size": "107", "standard_uom": "GM", "standard_pack_size": None}, findings=["SIGNIFICANT_LEGACY_SIZE_MISMATCH"])
    nothing = row(2, "C", "UNRESOLVED", blank, context={"item_desc_eng": "GREEN TEA"}, reason="NOT_IN_DESCRIPTION")
    missed = row(3, "C", "UNRESOLVED", blank, context={"item_desc_eng": "GREEN TEA 500ML"}, reason="NOT_IN_DESCRIPTION")
    f = build([mismatch, nothing, missed], raised=[mismatch, nothing, missed])["measures"]["flag_precision"]
    reasons = {r["id"]: r["products"] for r in f["reasons"]}
    assert reasons["m_flag_conflict"] == 1 and reasons["m_flag_no_size"] == 1 and reasons["m_flag_missed"] == 1
    assert (f["products"], f["justified"], f["percent"]) == (3, 2, 66.7)
    assert (f["needs_review"], f["legacy_mismatch"], f["legacy_mismatch_with_proposal"]) == (1, 1, 1)
    assert (f["blank_comment"], f["with_proposal"]) == (0, 1)
    assert "not in the 95%" in f["reported"]
