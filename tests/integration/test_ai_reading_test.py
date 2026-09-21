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
    assert (ai["tested"], ai["agreed"], ai["awaiting_decision"], ai["no_answer"]) == (5, 2, 2, 1)
    assert ai["accuracy_percent"] == 40.0
    assert ai["run"]["model_id"] == "gemini-test"
    decisions = {row["id"]: row for row in report["decisions"]}
    assert decisions["ai_size_disagrees"]["products_affected"] == 1
    assert decisions["ai_size_disagrees"]["example"].startswith("3: “720G” works out to 720 GM")
    assert decisions["ai_unit_disagrees"]["products_affected"] == 1
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
