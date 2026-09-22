import re

import pytest

from app.agents.provider import (
    InferenceResponse, InferenceResult, ObservedMeasurement, ProviderMetadata,
)
from app.rules.registry import load_default_registry
from app.services.ai_reading_test_service import AiReadingTestBlocked, AiReadingTestService
from app.services.quality_service import QualityService


class FakeRepositories:
    def __init__(self, items, status="READY_FOR_REVIEW"):
        self.job = {"job_id": "j1", "status": status, "quality": {"stale": True}}
        self.items = items
        self.results = None

    def get_job(self, job_id):
        return self.job if job_id == "j1" else None

    def update_job(self, job_id, values):
        for key, value in values.items():
            target = self.job
            *parents, leaf = key.split(".")
            for parent in parents:
                target = target.setdefault(parent, {})
            target[leaf] = value

    def quality_items(self, job_id):
        return self.items

    def replace_ai_reading_results(self, job_id, results):
        self.results = [dict(result) for result in results]

    def ai_reading_results(self, job_id):
        return [dict(result) for result in self.results or []]


class ReadingProvider:
    """Reads the first Latin measurement; declines GREEN TEA; fails on BROKEN."""

    def __init__(self):
        self.requests = []

    async def infer(self, request):
        self.requests.append(request)
        text = request.item_desc_eng or ""
        if "BROKEN" in text:
            raise RuntimeError("provider unavailable")
        match = re.search(r"(\d+(?:\.\d+)?)\s*(KG|GM|G|ML|L)\b", text)
        if match and "UNREADABLE" not in text:
            result = InferenceResult(
                status="PROPOSAL", confidence="HIGH", reason_code="EXPLICIT_MEASUREMENT",
                measurement=ObservedMeasurement(
                    value=match.group(1), uom=match.group(2),
                    field="item_desc_eng", fragment=match.group(0),
                ),
            )
        else:
            result = InferenceResult(
                status="NOT_IN_DESCRIPTION", confidence="LOW", reason_code="NOT_IN_DESCRIPTION",
            )
        return InferenceResponse(result=result, metadata=ProviderMetadata(
            provider="google-adk", agent_name="a", agent_version="1.1.0",
            prompt_version="uom-inference-v2", model_id="gemini-test", latency_ms=12,
        ))


def item(item_no, desc, klm, group="A"):
    return {
        "item_no": item_no, "row_number": int(item_no) + 1, "group": group,
        "original": {"legacy_size": "999", "legacy_uom": "EA", "standard_size": klm[0],
                     "standard_uom": klm[1], "standard_pack_size": "1"},
        "context": {"item_desc_eng": desc}, "findings": [],
        "application_policy": "NO_CHANGE", "review": {"overall_status": "NOT_REQUIRED"},
    }


ITEMS = [
    item("1", "JUICE 500ML", ("500", "ML")),             # agrees
    item("2", "FLOUR 1KG", ("1000", "GM")),              # agrees after the rule engine converts
    item("3", "DUMPLING 720G", ("540", "GM")),           # differs in size
    item("4", "ICE CREAM 650G", ("650", "ML")),          # differs in kind of unit
    item("5", "UNREADABLE 300G", ("300", "GM")),         # AI gives no answer
    item("6", "BROKEN 100G", ("100", "GM")),             # call fails: not scored
    item("7", "GREEN TEA", ("1", "EA")),                 # no size in text: not eligible
    item("8", "SAUCE 200ML", ("200", "ML"), group="B"),  # not an answer key: not eligible
]


def service(repos, provider, real=True, registry=None):
    return AiReadingTestService(
        repos, provider, registry or load_default_registry(), provider_is_real=real,
    )


def test_the_ai_sees_description_text_only_and_is_scored_in_two_parts():
    repos, provider = FakeRepositories(ITEMS), ReadingProvider()
    runner = service(repos, provider)

    state, selected = runner.start("j1")
    assert (state["status"], state["total"], state["available"]) == ("RUNNING", 6, 6)
    assert provider.requests == []  # nothing is billed until run()
    runner.run("j1", selected)

    # Blindness: the request type has no field for entered or legacy values.
    sent = provider.requests[0].model_dump()
    assert set(sent) == {
        "task", "known_measurement", "item_brand_eng", "item_brand_local_lang",
        "item_desc_eng", "item_desc_local_lang", "web_description_eng", "web_description_chi",
        "division", "category", "subcategory", "section",  # D3: context, never evidence
    }
    assert sent["known_measurement"] is None

    run = repos.job["ai_reading_test"]
    assert (run["status"], run["processed"], run["errors"]) == ("COMPLETED", 6, 1)
    assert (run["model_id"], run["prompt_version"]) == ("gemini-test", "uom-inference-v2")
    assert repos.job["quality"] is None
    assert {row["item_no"]: row["outcome"] for row in repos.results} == {
        "1": "AGREES", "2": "AGREES", "3": "SIZE", "4": "UNIT", "5": "NO_ANSWER", "6": "ERROR",
    }

    report = QualityService(load_default_registry()).build_report(repos.job, ITEMS)
    ai = next(row for row in report["capabilities"] if row["key"] == "ai_description_reading")
    verdicts = {row["verdict"]: row["products"] for row in ai["breakdown"]}
    # 3: text says 720G, Excel 540 -> data problem. 4: text says 650G, Excel 650 ML -> data
    # problem. 5: the size is written and the AI missed it -> the one real miss.
    assert verdicts == {"CORRECT": 2, "AGENT_MISS": 1, "DATA_PROBLEM": 2}
    assert (ai["tested"], ai["agreed"], ai["match_percent"]) == (5, 2, 40.0)
    assert (ai["scored"], ai["correct"], ai["accuracy_percent"], ai["data_problems"]) == (3, 2, 66.7, 2)
    assert ai["run"]["model_id"] == "gemini-test"
    decisions = {row["id"]: row for row in report["decisions"]}
    # Data problems are not business-policy decisions, so they do not pad that list.
    assert decisions["ai_size_disagrees"]["products_affected"] == 0
    assert decisions["approve_ai_reading_test"]["products_affected"] == 0


def test_a_smaller_sample_limits_the_paid_calls():
    repos, provider = FakeRepositories(ITEMS), ReadingProvider()
    runner = service(repos, provider)
    state, selected = runner.start("j1", limit=2)
    runner.run("j1", selected)
    assert state["total"] == 2 and len(provider.requests) == 2


def test_start_is_refused_when_it_would_be_meaningless_or_duplicate():
    with pytest.raises(AiReadingTestBlocked, match="switched off"):
        service(FakeRepositories(ITEMS), ReadingProvider(), real=False).start("j1")
    with pytest.raises(AiReadingTestBlocked, match="not finished"):
        service(FakeRepositories(ITEMS, "PROCESSING"), ReadingProvider()).start("j1")
    with pytest.raises(AiReadingTestBlocked, match="No completed product"):
        service(FakeRepositories([ITEMS[6]]), ReadingProvider()).start("j1")
    with pytest.raises(LookupError):
        service(FakeRepositories(ITEMS), ReadingProvider()).start("other")

    repos = FakeRepositories(ITEMS)
    runner = service(repos, ReadingProvider())
    runner.start("j1")
    with pytest.raises(AiReadingTestBlocked, match="already running"):
        runner.start("j1")


class ChineseUnitProvider(ReadingProvider):
    """Reports the unit exactly as written in the local-language text."""

    async def infer(self, request):
        self.requests.append(request)
        match = re.search(r"(\d+)(克)", request.item_desc_local_lang or "")
        return InferenceResponse(
            result=InferenceResult(
                status="PROPOSAL", confidence="HIGH", reason_code="EXPLICIT_MEASUREMENT",
                measurement=ObservedMeasurement(
                    value=match.group(1), uom=match.group(2),
                    field="item_desc_local_lang", fragment=match.group(0),
                ),
            ),
            metadata=ProviderMetadata(provider="google-adk", agent_name="a", agent_version="1"),
        )


def test_a_local_language_unit_is_converted_not_discarded():
    product = item("1", None, ("140", "GM"))
    product["context"] = {"item_desc_local_lang": "黑芝麻穀物140克"}
    repos = FakeRepositories([product])
    runner = service(repos, ChineseUnitProvider())
    _, selected = runner.start("j1")
    runner.run("j1", selected)
    score = repos.job["ai_reading_test"]["score"]
    assert (score["tested"], score["agreed"], score["no_answer"]) == (1, 1, 0)


def test_stored_readings_are_rescored_for_free_after_a_rule_change(tmp_path):
    # A ruleset that does not know 克 yet: the correct reading is lost as "no answer".
    from app.rules.registry import DEFAULT_RULESET_PATH, RuleRegistry
    old_rules = tmp_path / "rules.yaml"
    old_rules.write_text(DEFAULT_RULESET_PATH.read_text(encoding="utf-8").replace("[G, 克]", "[G]"), encoding="utf-8")
    product = item("1", None, ("140", "GM"))
    product["context"] = {"item_desc_local_lang": "黑芝麻穀物140克"}
    repos, provider = FakeRepositories([product]), ChineseUnitProvider()
    runner = service(repos, provider, registry=RuleRegistry.load(old_rules))
    _, selected = runner.start("j1")
    runner.run("j1", selected)
    assert repos.job["ai_reading_test"]["score"]["no_answer"] == 1

    repos.job["quality"] = {"stale": True}
    rescored = service(repos, provider).rescore("j1")

    assert (rescored["agreed"], rescored["no_answer"]) == (1, 0)
    assert len(provider.requests) == 1  # no new paid call
    assert repos.job["quality"] is None


def test_only_products_without_an_answer_are_retested():
    repos, provider = FakeRepositories(ITEMS), ReadingProvider()
    runner = service(repos, provider)
    _, selected = runner.start("j1")
    runner.run("j1", selected)
    first_calls = len(provider.requests)

    state, retry = runner.start("j1", only_unanswered=True)
    assert sorted(product["item_no"] for product in retry) == ["5", "6"]
    assert (state["mode"], state["total"]) == ("UNANSWERED", 2)
    runner.run("j1", retry, only_unanswered=True)

    assert len(provider.requests) == first_calls + 2
    assert len(repos.results) == 6  # earlier readings are kept, not re-bought
    score = repos.job["ai_reading_test"]["score"]
    assert (score["tested"], score["agreed"]) == (5, 2)


class V3Provider(ReadingProvider):
    """Answers with roles, as agent contract v3 does."""

    ANSWERS = {
        "1L MICROWAVE BOX": ("1", "L", "1L", "CAPACITY_OR_RANGE", None, None),
        "CHEESE 180G(10GX18)": ("10", "G", "10G", "NET_CONTENT_UNIT", "18", "SELLABLE_PACK"),
        "PASTA 500KG": ("500", "KG", "500KG", "NET_CONTENT_UNIT", None, None),
    }

    async def infer(self, request):
        self.requests.append(request)
        value, uom, fragment, role, pack, pack_role = self.ANSWERS[request.item_desc_eng]
        from app.agents.provider import Evidence
        return InferenceResponse(
            result=InferenceResult(
                status="PROPOSAL", rationale="test",
                measurement=ObservedMeasurement(value=value, uom=uom, field="item_desc_eng", fragment=fragment, role=role),
                pack_size=pack, pack_role=pack_role,
                pack_evidence=Evidence(field="item_desc_eng", fragment="10GX18") if pack else None,
            ),
            metadata=ProviderMetadata(provider="google-adk", agent_name="a", agent_version="2.0.0",
                                      prompt_version="uom-inference-v3", input_tokens=1000, output_tokens=50),
        )


def test_v3_scoring_rewards_the_right_behaviour():
    pasta_shelf = [item(str(100 + n), "PASTA 500G", ("500", "GM")) for n in range(20)]
    for row in pasta_shelf:
        row["context"]["category"] = "Pasta"
    box = item("1", "1L MICROWAVE BOX", ("10", "EA"))
    cheese = item("2", "CHEESE 180G(10GX18)", ("180", "GM"))
    pasta = item("3", "PASTA 500KG", ("500", "GM"))
    pasta["context"]["category"] = "Pasta"
    repos = FakeRepositories([box, cheese, pasta, *pasta_shelf])
    repos.quality_items = lambda job_id: [box, cheese, pasta]  # only these are called
    runner = service(repos, V3Provider())
    _, selected = runner.start("j1")
    repos.quality_items = lambda job_id: [box, cheese, pasta, *pasta_shelf]  # scoring sees the shelf
    runner.run("j1", selected)

    outcomes = {row["item_no"]: row["outcome"] for row in repos.results}
    assert outcomes == {"1": "AGREES", "2": "AGREES", "3": "SENT_TO_REVIEW"}
    notes = {e["item_no"]: e["note"] for e in repos.job["ai_reading_test"]["score"]["examples"]}
    assert "capacity" in notes["1"] and "records this product as a count" in notes["1"]
    assert notes["2"].startswith("Same total.")
    assert "far outside the normal sizes" in notes["3"]
    score = repos.job["ai_reading_test"]["score"]
    assert (score["tested"], score["agreed"], score["no_answer"], score["disagreements"]) == (3, 2, 1, {})

    history = repos.job["ai_reading_history"]
    assert len(history) == 1
    assert (history[0]["prompt_version"], history[0]["accuracy_percent"], history[0]["input_tokens"]) == (
        "uom-inference-v3", 66.7, 3000,
    )


def test_unanswered_retest_is_refused_when_the_instructions_changed():
    repos = FakeRepositories(ITEMS)
    provider = ReadingProvider()
    runner = service(repos, provider)
    _, selected = runner.start("j1")
    runner.run("j1", selected)
    provider.bundle = type("Bundle", (), {"prompt_version": "uom-inference-v3"})()
    with pytest.raises(AiReadingTestBlocked, match="instructions changed"):
        runner.start("j1", only_unanswered=True)


def test_declining_is_correct_when_the_true_size_is_not_written():
    class Decliner(ReadingProvider):
        async def infer(self, request):
            self.requests.append(request)
            return InferenceResponse(
                result=InferenceResult(status="NOT_IN_DESCRIPTION", rationale="4L is a grade"),
                metadata=ProviderMetadata(provider="google-adk", agent_name="a", agent_version="2"),
            )

    grade = item("1", "ORG BSM 4L VINEGAR", ("250", "ML"))     # 250 ML is written nowhere
    missed = item("2", "ORANGE JUICE 250ML", ("250", "ML"))    # the size IS written: a real miss
    repos = FakeRepositories([grade, missed])
    runner = service(repos, Decliner())
    _, selected = runner.start("j1")
    runner.run("j1", selected)
    assert {row["item_no"]: row["outcome"] for row in repos.results} == {"1": "AGREES", "2": "NO_ANSWER"}
    note = next(e["note"] for e in repos.job["ai_reading_test"]["score"]["examples"] if e["item_no"] == "1")
    assert "is not written in the text" in note


def test_bundles_and_case_totals_are_the_agreed_rule_not_a_disagreement():
    class Bundles(V3Provider):
        ANSWERS = {
            "SOY SAUCE 500MLx2": ("500", "ML", "500ML", "NET_CONTENT_UNIT", "2", "SELLABLE_PACK"),
            "NOODLE 5 CASE/6 X 90GM": ("90", "GM", "90GM", "NET_CONTENT_UNIT", "6", "OUTER_CASE"),
        }

        async def infer(self, request):
            response = await super().infer(request)
            if response.result.pack_evidence:
                response.result.pack_evidence.fragment = "2" if "SOY" in request.item_desc_eng else "6"
            return response

    twin = item("1", "SOY SAUCE 500MLx2", ("1", "EA"))          # team recorded a count
    case = item("2", "NOODLE 5 CASE/6 X 90GM", ("450", "GM"))   # team recorded 5 x 90
    repos = FakeRepositories([twin, case])
    runner = service(repos, Bundles())
    _, selected = runner.start("j1")
    runner.run("j1", selected)
    score = repos.job["ai_reading_test"]["score"]
    assert score["verdicts"] == {"RULE_APPLIED": 2}
    assert all("agreed rule" in example["note"] for example in score["examples"])
