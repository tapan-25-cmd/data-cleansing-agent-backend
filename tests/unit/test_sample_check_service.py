"""A fixed, stratified sample per group; a second model's verdicts become one figure per group."""
import pytest

from app.agents.judge import Evidence, JudgeRequest, JudgeResult, MockJudgeProvider, ToolAction, Values, validate_judgement
from app.services.sample_check_service import SampleCheckService, draw_sample, figure, judge_request


def row(n, route, policy, desc="SUGAR", proposals=None, findings=()):
    return {"row_number": n, "item_no": f"{n:06d}", "route": route, "application_policy": policy,
            "context": {"item_desc_eng": desc, "category": "Sugar"},
            "original": {"legacy_size": "500", "legacy_uom": "GM", "standard_size": 500, "standard_uom": "GM", "standard_pack_size": 1},
            "field_proposals": proposals or {"standard_size": None, "standard_uom": None, "standard_pack_size": None},
            "findings": [{"code": c} for c in findings], "changes": [], "review": {"overall_status": "NOT_REQUIRED"}}


def test_the_draw_is_repeatable_and_takes_a_floor_from_every_set():
    items = [row(n, "A", "NO_CHANGE") for n in range(1, 101)]
    first = draw_sample("job", items, "A", 10)
    assert first == draw_sample("job", items, "A", 10) and len(first) == 10
    assert draw_sample("other", items, "A", 10) != first
    membership = {"a_legacy_exact": list(range(1, 97)), "a_text_confirms": [97, 98], "a_unverified": [99, 100], "b_x": [1]}
    stratified = draw_sample("job", items, "A", 10, membership)
    assert len(stratified) == 10 and {97, 98} <= set(stratified) and {99, 100} <= set(stratified)
    # Only rows of the asked group are drawn.
    assert draw_sample("job", items + [row(500, "B", "AUTO_APPLY")], "B", 5) == [500]


def test_the_figure_needs_enough_verdicts_and_leaves_cant_tell_beside_it():
    f = figure(["RIGHT"] * 27 + ["WRONG"] * 3 + ["CANT_TELL"] * 2)
    assert (f["right"], f["wrong"], f["cant_tell"], f["judged"], f["percent"]) == (27, 3, 2, 30, 90.0)
    assert f["margin"] == 10.7 and not f["too_few"]
    thin = figure(["RIGHT"] * 5)
    assert thin["too_few"] and thin["percent"] is None


def test_the_request_asks_the_groups_question_and_shows_the_tool_action():
    kept = judge_request(row(1, "A", "NO_CHANGE"))
    assert kept.question == "KEEP" and kept.excel.size == "500" and kept.legacy.uom == "GM" and kept.tool.final.size is None
    assert kept.category_kind == "UNKNOWN"
    assert judge_request(row(1, "A", "NO_CHANGE"), {"mixed_categories": ["Sugar"]}).category_kind == "MIXED"
    assert judge_request(row(1, "A", "NO_CHANGE"), {"liquid_categories": ["Sugar"]}).category_kind == "LIQUID"
    # The tool's own ounce finding wins over the category-level profile.
    assert judge_request(row(1, "A", "REVIEW_REQUIRED", findings=["OUNCE_MAY_BE_FLUID"]), {"liquid_categories": ["Sugar"]}).category_kind == "MIXED"
    changed = judge_request(row(2, "B", "AUTO_APPLY", proposals={"standard_size": "1000", "standard_uom": "GM", "standard_pack_size": None}))
    assert changed.question == "CHANGE" and changed.tool.final.size == "1000"
    raised = judge_request(row(3, "A", "REVIEW_REQUIRED", findings=["SIGNIFICANT_LEGACY_SIZE_MISMATCH"]))
    assert raised.question == "RAISE" and raised.tool.label == "Needs your review"


def test_a_judgement_on_the_words_must_quote_words_that_are_there():
    request = JudgeRequest(question="KEEP", descriptions={"item_desc_eng": "JUICE 500ML", "item_brand_eng": "BRAND 7"},
                           excel=Values(size="500", uom="ML"), tool=ToolAction(final=Values(), label="Already correct", reason="ok"))
    validate_judgement(request, JudgeResult(verdict="RIGHT", basis="DESCRIPTION", reason="", evidence=[Evidence(field="item_desc_eng", fragment="500ML")]))
    with pytest.raises(ValueError):
        validate_judgement(request, JudgeResult(verdict="RIGHT", basis="DESCRIPTION", reason="", evidence=[]))
    with pytest.raises(ValueError):
        validate_judgement(request, JudgeResult(verdict="RIGHT", basis="DESCRIPTION", reason="", evidence=[Evidence(field="item_desc_eng", fragment="750ML")]))
    with pytest.raises(ValueError):
        validate_judgement(request, JudgeResult(verdict="RIGHT", basis="DESCRIPTION", reason="", evidence=[Evidence(field="item_brand_eng", fragment="7")]))
    with pytest.raises(ValueError):
        validate_judgement(request, JudgeResult(verdict="CANT_TELL", basis="OLD_SIZE", reason=""))


def test_a_run_gives_one_figure_per_group_and_keeps_every_verdict():
    sampled = {"A": [row(n, "A", "NO_CHANGE") for n in range(1, 25)] + [row(25, "A", "NO_CHANGE", desc="WRONG SIZE")],
               "B": [row(n, "B", "AUTO_APPLY", proposals={"standard_size": "1000", "standard_uom": "GM", "standard_pack_size": None}) for n in range(30, 52)],
               "C": []}
    report = SampleCheckService(MockJudgeProvider(), 3).run("job", sampled)
    by = {g["group"]: g for g in report["groups"]}
    assert (by["A"]["right"], by["A"]["wrong"], by["A"]["percent"]) == (24, 1, 96.0)
    assert by["B"]["right"] == 22 and by["C"]["judged"] == 0 and by["C"]["too_few"]
    assert report["calls"] == 47 and report["model_id"] == "mock-judge"
    wrong = next(r for r in report["rows"] if r["verdict"] == "WRONG")
    assert wrong["verdict_label"] == "Should have changed" and wrong["evidence"][0]["fragment"] == "WRONG" and wrong["tool_label"] == "Already correct"
