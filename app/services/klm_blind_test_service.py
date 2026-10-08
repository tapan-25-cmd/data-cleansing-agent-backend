"""K L M blind test: are the unit size, unit and pack size in our output right?

100 rows are drawn at random from each outcome group (A left unchanged, B corrected, C raised
for a person). A blind checker, a second and stronger model, works out the right K, L and M
for each row from the raw data alone: it is never shown what the tool did, nor the group.
Plain rules then compare the two and mark each row Right, Wrong or Can't tell. A person can
add their own Yes/No beside every row.

What "right" means:
  A  the values kept are the values the checker reaches.
  B  the values written are the values the checker reaches.
  C  the suggested values are the checker's; with no suggestion, the row is right when the
     checker also finds Excel wrong, empty, or impossible to settle without a person.
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from typing import Any, Callable

from app.agents.judge import Values
from app.agents.klm_checker import KLM_CHECKER_VERSION, CheckRequest
from app.services.result_status import GROUP_NAMES, outcome_group
from app.services.sample_check_service import GROUPS, _ounce_kind, wilson
from app.services.sample_test_service import base_row
from app.services.web_check_service import _close, tool_amount

KLM_BLIND_TEST_VERSION = "klm-blind-test-v1"
DEFAULT_SIZE = 100
DEFAULT_SAMPLES = 3
RIGHT, WRONG, CANT_TELL = "RIGHT", "WRONG", "CANT_TELL"
ERROR_TYPES = {
    "MISSED_ERROR": "Missed error",          # A: kept a value that needed changing
    "WRONG_CORRECTION": "Wrong correction",  # B: wrote a value that is not right
    "FALSE_ALARM": "False alarm",            # C: raised a row whose values were right
    "WRONG_SUGGESTION": "Wrong suggestion",  # C: raised rightly, but suggested the wrong value
}
QUESTION = {
    "A": "We left K, L and M unchanged. Are they right?",
    "B": "We corrected K, L or M. Are the new values right?",
    "C": "We raised the row for a person. Is our suggested value right, or did the row really need a person?",
}
_FIELDS = ("standard_size", "standard_uom", "standard_pack_size")
_DESCRIPTIONS = ("item_brand_eng", "item_brand_local_lang", "item_desc_eng", "item_desc_local_lang",
                 "web_description_eng", "web_description_chi")


def _s(value: object) -> str | None:
    return None if value in (None, "") else str(value)


def draw(items_by_group: dict[str, list[dict[str, Any]]], size: int, seed: int) -> dict[str, list[dict[str, Any]]]:
    """``size`` rows at random from each group, the same rows for the same seed."""
    rng = random.Random(seed)
    out = {}
    for g in GROUPS:
        live = [x for x in items_by_group.get(g, []) if outcome_group(x) == g]
        out[g] = sorted(rng.sample(live, min(size, len(live))), key=lambda x: int(x["row_number"]))
    return out


def draw_more(items_by_group: dict[str, list[dict[str, Any]]], size: int, seed: int, samples: int,
              used: set[int] | None = None) -> list[dict[str, list[dict[str, Any]]]]:
    """``samples`` further samples of ``size`` rows per group, none sharing a row with another
    or with ``used`` (rows of samples already drawn). Each group is shuffled once and cut into
    consecutive pieces, so the samples cannot overlap. A group too small for every sample
    gives the later ones what is left."""
    rng = random.Random(seed)
    used = used or set()
    out: list[dict[str, list[dict[str, Any]]]] = [{} for _ in range(samples)]
    for g in GROUPS:
        live = [x for x in items_by_group.get(g, []) if outcome_group(x) == g and int(x["row_number"]) not in used]
        order = rng.sample(live, len(live))
        for k in range(samples):
            out[k][g] = sorted(order[k * size:(k + 1) * size], key=lambda x: int(x["row_number"]))
    return out


def sample_numbers(test: dict[str, Any]) -> list[int]:
    return sorted({int(r.get("sample") or 1) for r in test.get("rows") or []})


def rows_of(test: dict[str, Any], sample: int | None = None) -> list[dict[str, Any]]:
    rows = test.get("rows") or []
    return rows if sample is None else [r for r in rows if int(r.get("sample") or 1) == sample]


def check_request(item: dict[str, Any], profile: dict[str, Any] | None = None) -> CheckRequest:
    """What the checker sees: the raw data only. Nothing the tool produced is in here."""
    context, original = item.get("context") or {}, item.get("original") or {}
    return CheckRequest(
        descriptions={f: context.get(f) for f in _DESCRIPTIONS},
        category=context.get("category"), subcategory=context.get("subcategory"),
        category_kind=_ounce_kind(item, profile),
        legacy=Values(size=_s(original.get("legacy_size")), uom=original.get("legacy_uom")) if original.get("legacy_uom") else None,
        excel=Values(size=_s(original.get("standard_size")), uom=_s(original.get("standard_uom")), pack_size=_s(original.get("standard_pack_size"))),
    )


def say(values: dict[str, Any] | None) -> str:
    if not values or not values.get("standard_size"):
        return "blank"
    pack = values.get("standard_pack_size")
    return f"{values['standard_size']} {values.get('standard_uom') or ''}".strip() + (f" × {pack}" if pack not in (None, "") else "")


def same(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    """The same amount in the same kind of unit: 70 GM × 3 equals 210 GM × 1 in total, and
    4 EA × 1 equals 1 EA × 4. How a total is split between K and M is an open business
    question, so the split alone never makes a row wrong."""
    x, y = tool_amount(a or {}), tool_amount(b or {})
    return bool(x and y and x.uom == y.uom and _close(x.total, y.total))


def compare(group: str, row: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    """Right, Wrong or Can't tell for one row, the kind of error, and a sentence saying why."""
    def out(verdict: str, why: str, error: str | None = None) -> dict[str, Any]:
        return {"verdict": verdict, "error_type": error, "error_label": ERROR_TYPES.get(error or ""), "why": why}

    if check.get("error"):
        return out(CANT_TELL, "The checker could not answer for this row.")
    decision = check.get("decision")
    theirs = check.get("values")
    excel, final, suggestion = row.get("excel"), row.get("final"), row.get("suggestion")
    if group == "C":
        if decision == "NEEDS_PERSON":
            return out(RIGHT, "The checker also finds the file cannot settle the values: a person is needed, so raising the row was right.")
        if suggestion and same(suggestion, theirs):
            return out(RIGHT, f"We suggested {say(suggestion)}; the checker reaches the same value.")
        if same(excel, theirs):
            return out(WRONG, f"The checker finds Excel right as it stands ({say(excel)}); the row did not need a person.", "FALSE_ALARM")
        if suggestion:
            return out(WRONG, f"Raising was right (Excel {say(excel)} is not right), but we suggested {say(suggestion)} and the checker reaches {say(theirs)}.", "WRONG_SUGGESTION")
        return out(RIGHT, f"Excel holds {say(excel)}; the checker reaches {say(theirs)}, so the row did need a look.")
    if decision == "NEEDS_PERSON":
        return out(CANT_TELL, "The checker finds the file cannot settle the right values without a person, so it cannot say whether ours are right.")
    if same(final, theirs):
        return out(RIGHT, f"Our value {say(final)}; the checker reaches the same value on its own.")
    if group == "A":
        return out(WRONG, f"We kept {say(final)}; the checker reaches {say(theirs)}.", "MISSED_ERROR")
    return out(WRONG, f"We wrote {say(final)}; the checker reaches {say(theirs)}.", "WRONG_CORRECTION")


def figure(verdicts: list[str]) -> dict[str, Any]:
    right, wrong, cant = verdicts.count(RIGHT), verdicts.count(WRONG), verdicts.count(CANT_TELL)
    n = right + wrong
    out = {"right": right, "wrong": wrong, "cant_tell": cant, "checked": n, "sample": len(verdicts), "percent": None, "low": None, "high": None}
    if n:
        low, high = wilson(right / n, n)
        out.update(percent=round(100 * right / n, 1), low=round(100 * low, 1), high=round(100 * high, 1))
    return out


def summarize(test: dict[str, Any], sample: int | None = None) -> dict[str, Any]:
    """Per group and for all rows: the checker's figure, and the reviewer's where rows are
    marked. ``overall`` counts every checked row once (the three groups in equal numbers);
    ``weighted`` weights each group by its share of the workbook. ``sample`` limits it to one
    sample; without it every sample is pooled."""
    rows = rows_of(test, sample)
    populations = test.get("populations") or {}
    groups = []
    for g in GROUPS:
        mine = [r for r in rows if r["group"] == g]
        f = figure([r["verdict"] for r in mine])
        person = [("RIGHT" if (r.get("person") or {}).get("answer") == "YES" else "WRONG") for r in mine if (r.get("person") or {}).get("answer") in ("YES", "NO")]
        both = [r for r in mine if (r.get("person") or {}).get("answer") in ("YES", "NO") and r["verdict"] in (RIGHT, WRONG)]
        agree = sum(1 for r in both if (r["person"]["answer"] == "YES") == (r["verdict"] == RIGHT))
        groups.append({"group": g, "name": GROUP_NAMES[g], "question": QUESTION[g], "population": populations.get(g, 0), **f,
                       "errors": {k: sum(1 for r in mine if r.get("error_type") == k) for k in ERROR_TYPES if any(r.get("error_type") == k for r in mine)},
                       "person": figure(person), "agreement": {"both": len(both), "agree": agree}})
    overall = figure([r["verdict"] for r in rows])
    weighted = None
    total = sum(g["population"] for g in groups)
    if total and all(g["percent"] is not None for g in groups):
        weighted = round(sum(g["population"] / total * g["percent"] for g in groups), 1)
    person_all = [("RIGHT" if r["person"]["answer"] == "YES" else "WRONG") for r in rows if (r.get("person") or {}).get("answer") in ("YES", "NO")]
    return {"groups": groups, "overall": overall, "weighted_percent": weighted, "person": figure(person_all)}


class KlmBlindTestService:
    def __init__(self, checker: Any, concurrency: int = 6, category_profile: dict[str, Any] | None = None):
        self.checker = checker
        self.concurrency = concurrency
        self.category_profile = category_profile

    async def _check_all(self, items: list[dict[str, Any]], on_done: Callable[[int], None]) -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(self.concurrency)
        done = 0
        out: list[dict[str, Any]] = [None] * len(items)  # type: ignore[list-item]

        async def one(index: int, item: dict[str, Any]) -> None:
            nonlocal done
            try:
                async with semaphore:
                    response = await self.checker.check(check_request(item, self.category_profile))
                r = response.result
                values = None if r.decision == "NEEDS_PERSON" else {
                    "standard_size": r.size, "standard_uom": r.uom, "standard_pack_size": r.pack_size or "1"}
                out[index] = {"decision": r.decision, "values": values, "reason": r.reason, "confidence": r.confidence,
                              "evidence": [e.model_dump() for e in r.evidence], "model_id": response.model_id,
                              "prompt_version": response.prompt_version, "prompt_sha256": response.prompt_sha256,
                              "tokens": {"input": response.input_tokens, "output": response.output_tokens}}
            except Exception as exc:  # noqa: BLE001 - one row never stops the test
                out[index] = {"decision": None, "values": None, "reason": "", "evidence": [],
                              "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            done += 1
            on_done(done)

        await asyncio.gather(*(one(i, item) for i, item in enumerate(items)))
        return out

    def run(self, sampled: dict[str, list[dict[str, Any]]], on_done: Callable[[int], None] = lambda n: None,
            sample: int = 1) -> list[dict[str, Any]]:
        return self.run_samples([(sample, sampled)], on_done)

    def run_samples(self, samples: list[tuple[int, dict[str, list[dict[str, Any]]]]],
                    on_done: Callable[[int], None] = lambda n: None) -> list[dict[str, Any]]:
        """Check several samples in one pass; every row carries its sample number."""
        flat = [(number, item) for number, sampled in samples for g in GROUPS for item in sampled.get(g, [])]
        checks = asyncio.run(self._check_all([item for _, item in flat], on_done))
        rows = []
        for (number, item), check in zip(flat, checks):
            row = base_row(item)
            rows.append({**row, "sample": number, "check": check, **compare(row["group"], row, check),
                         "person": {"answer": None, "comment": ""}})
        return rows


def new_test(job_id: str, size: int, seed: int, populations: dict[str, int]) -> dict[str, Any]:
    return {"job_id": job_id, "version": KLM_BLIND_TEST_VERSION, "checker_version": KLM_CHECKER_VERSION, "size": size, "seed": seed,
            "populations": populations, "started_at": datetime.now(timezone.utc)}
