"""The reasoning layer as a pipeline stage: which rows it reads, what the gate lets its
answer do, and that a failure never stops a run."""
from decimal import Decimal

from app.agents.reconcile import Evidence, Proposed, ReconcileResponse, ReconcileResult
from app.services.reasoning_stage import ReasoningStage, gate, triggered
from app.services.result_ledger_service import enrich_result_item
from app.services.result_status import effective_status, outcome_group


def kept_row(n=1, **changes):
    """A kept row the checker raised: Excel 100 GM, the name says 3.3G (a nutrient figure)."""
    row = {
        "row_number": n, "item_no": f"{n:06d}", "route": "A",
        "context": {"item_desc_eng": "3.3G YOGHURT", "category": "Yoghurts"},
        "original": {"legacy_size": "100", "legacy_uom": "GM", "standard_size": 100, "standard_uom": "GM", "standard_pack_size": 1},
        "field_proposals": {"standard_size": None, "standard_uom": None, "standard_pack_size": None},
        "validation": {"issues": [{"code": "DESCRIPTION_SIZE_DIFFERS", "field": "item_desc_eng", "severity": "WARNING",
                                   "message": "The description states a different size", "current_value": "3.3G", "expected_value": "100 GM"}]},
        "review": {"overall_status": "NOT_REQUIRED", "field_decisions": {}},
    }
    row.update(changes)
    return row


def response(verdict="EXCEL_RIGHT", size="100", uom="GM", pack="1", confidence="HIGH", fragment="3.3G YOGHURT",
             needs_rule=False, knowledge=None):
    proposed = None if verdict == "CANNOT_TELL" else Proposed(standard_size=Decimal(size), standard_uom=uom, standard_pack_size=Decimal(pack))
    result = ReconcileResult(product_unit="one tub", verdict=verdict, proposed=proposed, explanation="3.3G is the protein figure.",
                             evidence=[Evidence(field="item_desc_eng", fragment=fragment)] if fragment else [],
                             needs_business_rule=needs_rule, used_product_knowledge=knowledge, confidence=confidence)
    return ReconcileResponse(result=result, model_id="fake", prompt_version="uom-reconcile-v3", prompt_sha256="x",
                             latency_ms=5, input_tokens=100, output_tokens=20, attempts=1)


class FakeReasoner:
    model_id = "fake"

    def __init__(self, answer=None, fail_rows=()):
        self.answer = answer or response()
        self.fail_rows = set(fail_rows)
        self.calls = 0

    async def reconcile(self, request):
        self.calls += 1
        if "FAIL" in (request.descriptions.get("item_desc_eng") or ""):
            raise RuntimeError("provider down")
        return self.answer


def test_only_rows_where_a_rule_interpreted_a_number_are_read():
    raised = enrich_result_item(kept_row())
    assert triggered(raised)
    plain = enrich_result_item(kept_row(validation={"issues": []}))
    assert not triggered(plain)
    read_blind = enrich_result_item(kept_row(route="C"))
    assert not triggered(read_blind), "rows the description reader handled are not read again"


def test_the_gate_maps_each_kind_of_answer():
    row = enrich_result_item(kept_row())
    assert gate(row, response())["outcome"] == "CONFIRMED"
    assert gate(row, response(confidence="MEDIUM"))["outcome"] == "AGREES"
    # Product-kind knowledge is allowed; an open business rule or a missing quote is not.
    assert gate(row, response(knowledge="3.3G in the name is protein"))["outcome"] == "CONFIRMED"
    assert gate(row, response(needs_rule=True))["outcome"] == "AGREES"
    assert gate(row, response(fragment=None))["outcome"] == "AGREES"
    assert gate(row, response(verdict="DESCRIPTION_RIGHT", size="120"))["outcome"] == "DISAGREES"
    assert gate(row, response(verdict="DESCRIPTION_RIGHT", size="120", confidence="MEDIUM"))["outcome"] == "SUGGESTS"
    assert gate(row, response(verdict="CANNOT_TELL"))["outcome"] == "CANNOT_TELL"
    # A total changed with no source for it is never acted on.
    assert gate(row, response(verdict="COMBINED", size="100", pack="3"))["outcome"] == "SUGGESTS"


def test_in_gate_mode_a_confirmation_clears_the_raise_and_keeps_it_for_audit():
    decision = gate(enrich_result_item(kept_row()), response())
    shadow = enrich_result_item(kept_row(reasoner={**decision, "mode": "shadow"}))
    assert effective_status(shadow) == "REVIEW_REQUIRED", "shadow changes nothing"
    acted = enrich_result_item(kept_row(reasoner={**decision, "mode": "gate"}))
    assert effective_status(acted) == "OBSERVATION_ONLY" and outcome_group(acted) == "A"
    codes = {f["code"] for f in acted["findings"]}
    assert "DESCRIPTION_SIZE_DIFFERS" not in codes and "REASONER_CONFIRMED" in codes
    assert acted["reasoner"]["resolved"] == ["DESCRIPTION_SIZE_DIFFERS"]
    assert "protein" in next(f["human_reason"] for f in acted["findings"] if f["code"] == "REASONER_CONFIRMED")


def test_in_gate_mode_a_confident_disagreement_raises_a_kept_row_with_the_ai_values():
    kept = kept_row(validation={"issues": [{"code": "DESCRIPTION_MEASUREMENT_MISMATCH", "field": "item_desc_eng",
                                            "severity": "WARNING", "message": "a number in another unit"}]},
                    context={"item_desc_eng": "OIL 900G", "category": "Oils"})
    before = enrich_result_item(kept)
    assert effective_status(before) == "OBSERVATION_ONLY"
    decision = gate(before, response(verdict="DESCRIPTION_RIGHT", size="900", fragment="900G"))
    assert decision["outcome"] == "DISAGREES"
    after = enrich_result_item({**kept, "reasoner": {**decision, "mode": "gate"}})
    assert effective_status(after) == "REVIEW_REQUIRED" and outcome_group(after) == "C"
    assert after["field_proposals"]["standard_size"] == "900"
    size = next(c for c in after["changes"] if c["field"] == "standard_size")
    assert (size["proposed"], size["final"]) == ("900", 100), "the AI suggests; it never writes"


def test_a_failed_call_leaves_the_row_alone_and_the_run_goes_on():
    rows = [enrich_result_item(kept_row(1)), enrich_result_item(kept_row(2, context={"item_desc_eng": "FAIL 3.3G YOGHURT"}))]
    decisions, stats = ReasoningStage(FakeReasoner(), "gate").run(rows)
    assert decisions[0]["outcome"] == "CONFIRMED" and decisions[1]["outcome"] == "UNAVAILABLE"
    assert "provider down" in decisions[1]["error"]
    assert stats["outcomes"] == {"CONFIRMED": 1, "UNAVAILABLE": 1} and stats["reasoned"] == 2
    still = enrich_result_item(kept_row(2, reasoner={**decisions[1], "mode": "gate"}))
    assert effective_status(still) == "REVIEW_REQUIRED"


def test_the_budget_cap_and_off_mode():
    rows = [enrich_result_item(kept_row(n)) for n in range(1, 6)]
    reasoner = FakeReasoner()
    decisions, stats = ReasoningStage(reasoner, "shadow", max_rows=3).run(rows)
    assert len(decisions) == 3 and reasoner.calls == 3 and stats["capped"] and stats["triggered"] == 5
    assert ReasoningStage(reasoner, "off").run(rows) == ({}, {"mode": "off", "triggered": 0, "reasoned": 0})


def converted_row(desc, legacy=("1", "LT"), proposals=None, original_extra=None, n=7):
    return {
        "row_number": n, "item_no": f"{n:06d}", "route": "B", "reason_code": "RULE_CONVERSION",
        "context": {"item_desc_eng": desc, "category": "Soup"},
        "original": {"legacy_size": legacy[0], "legacy_uom": legacy[1], "standard_size": None, "standard_uom": None,
                     "standard_pack_size": 1, **(original_extra or {})},
        "field_proposals": proposals or {"standard_size": "1000", "standard_uom": "ML", "standard_pack_size": None},
        "field_provenance": {"standard_size": {"method": "RULE", "rule_id": "LT_TO_ML"}},
        "review": {"overall_status": "NOT_REQUIRED", "field_decisions": {}},
    }


def test_a_count_in_the_name_that_differs_from_the_pack_written_triggers_a_second_reading():
    row = enrich_result_item(converted_row("CHICKEN BROTH\\3(AUS)"))
    assert effective_status(row) == "AUTO_APPLY", "the note alone changes nothing"
    assert "TEXT_COUNT_DIFFERS_FROM_PACK" in {f["code"] for f in row["findings"]} and triggered(row)
    # Three pieces already held as 3 EA × 1, and a name with no count, do not trigger.
    ea = enrich_result_item(converted_row("OREO STICK 3S", legacy=("3", "PC"),
                                          proposals={"standard_size": "3", "standard_uom": "EA", "standard_pack_size": None}))
    assert not triggered(ea)
    assert not triggered(enrich_result_item(converted_row("CHICKEN BROTH")))


def test_a_written_value_that_rests_on_an_open_question_goes_to_a_person():
    coupon = enrich_result_item(converted_row("LAVA CUSTARD 4S CPN", legacy=("1", "PC"),
                                              proposals={"standard_size": "1", "standard_uom": "EA", "standard_pack_size": "1"}))
    answer = response(verdict="DESCRIPTION_RIGHT", size="1", uom="EA", pack="4", fragment="4S", needs_rule=True)
    decision = gate(coupon, answer)
    assert decision["outcome"] == "OPEN_DECISION"
    raw = converted_row("LAVA CUSTARD 4S CPN", legacy=("1", "PC"), proposals={"standard_size": "1", "standard_uom": "EA", "standard_pack_size": "1"})
    after = enrich_result_item({**raw, "reasoner": {**decision, "mode": "gate"}})
    assert effective_status(after) == "REVIEW_REQUIRED" and outcome_group(after) == "C"
    assert "REASONER_OPEN_DECISION" in {f["code"] for f in after["findings"]}
    # A confident, quoted disagreement on a converted row raises it too, with the AI's pack.
    broth = enrich_result_item(converted_row("CHICKEN BROTH\\3(AUS)"))
    disagree = gate(broth, response(verdict="DESCRIPTION_RIGHT", size="1000", uom="ML", pack="3", fragment="\\3"))
    assert disagree["outcome"] == "DISAGREES"
    raised = enrich_result_item({**converted_row("CHICKEN BROTH\\3(AUS)"), "reasoner": {**disagree, "mode": "gate"}})
    assert effective_status(raised) == "REVIEW_REQUIRED"


def test_a_written_value_needs_a_second_reading_that_supports_it():
    coupon_raw = converted_row("LAVA CUSTARD 4S CPN", legacy=("1", "PC"),
                               proposals={"standard_size": "1", "standard_uom": "EA", "standard_pack_size": "1"})
    coupon = enrich_result_item(coupon_raw)
    # The AI agrees with 1 EA × 1 but flags the open coupon question: a person decides.
    agrees_open = gate(coupon, response(verdict="EXCEL_RIGHT", size="1", uom="EA", pack="1", fragment="4S CPN", needs_rule=True))
    assert agrees_open["outcome"] == "OPEN_DECISION"
    assert effective_status(enrich_result_item({**coupon_raw, "reasoner": {**agrees_open, "mode": "gate"}})) == "REVIEW_REQUIRED"
    # The AI cannot tell whether the written value is right: a person decides.
    twin_raw = converted_row("FIRST DRAW TWIN PK", legacy=("1", "PK"),
                             proposals={"standard_size": "1", "standard_uom": "EA", "standard_pack_size": "1"})
    unsupported = gate(enrich_result_item(twin_raw), response(verdict="CANNOT_TELL", fragment=None))
    assert unsupported["outcome"] == "UNSUPPORTED"
    after = enrich_result_item({**twin_raw, "reasoner": {**unsupported, "mode": "gate"}})
    assert effective_status(after) == "REVIEW_REQUIRED" and "REASONER_UNSUPPORTED" in {f["code"] for f in after["findings"]}
    # On a kept row, "cannot tell" still changes nothing.
    assert gate(enrich_result_item(kept_row()), response(verdict="CANNOT_TELL", fragment=None))["outcome"] == "CANNOT_TELL"
