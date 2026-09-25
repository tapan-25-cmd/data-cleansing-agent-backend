"""Sample check: a fixed random sample per outcome group, judged by a second model (or a
person) on the group's one question, giving one figure per group with its margin.

The draw is repeatable (hash of job, group and row) so nobody can cherry-pick and later
verdicts land on the same rows. Inside a group the draw is stratified over our accuracy
sets, so small corners (fluid ounces, spelling fixes) are seen, with a floor per set and the
rest at random. The figure itself is unweighted: right ÷ (right + wrong), CANT_TELL beside it.
"""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from math import sqrt
from typing import Any, Callable, Iterable

from app.agents.judge import JUDGE_PROMPT_VERSION, JudgeRequest, ToolAction, Values
from app.services.job_comparison_service import outcome
from app.services.lane_a_trial_service import category_kind
from app.services.result_status import GROUP_NAMES, effective_status, outcome_group

SAMPLE_CHECK_VERSION = "sample-check-v1"
GROUPS = ("A", "B", "C")
QUESTION = {"A": "KEEP", "B": "CHANGE", "C": "RAISE"}
QUESTION_TEXT = {"A": "Was keeping the values right?", "B": "Was the change right?", "C": "Was raising it right?"}
VERDICT_WORDS = {
    "A": {"RIGHT": "Right to keep", "WRONG": "Should have changed", "CANT_TELL": "Can't tell"},
    "B": {"RIGHT": "Change is right", "WRONG": "Change is wrong", "CANT_TELL": "Can't tell"},
    "C": {"RIGHT": "Right to raise", "WRONG": "Should have decided itself", "CANT_TELL": "Can't tell"},
}
FLOOR_PER_SET = 2
MIN_FOR_PERCENT = 20


def _order_key(job_id: str, group: str, row: int) -> str:
    return sha256(f"{job_id}:{group}:{row}".encode()).hexdigest()


def draw_sample(job_id: str, items: Iterable[dict[str, Any]], group: str, size: int,
                membership: dict[str, list[int]] | None = None) -> list[int]:
    """Row numbers of the sample for one group: a floor from each of our sets, the rest by
    the fixed random order. Without a membership map the draw is plain random."""
    rows = [int(x["row_number"]) for x in items if outcome_group(x) == group]
    ordered = sorted(rows, key=lambda r: _order_key(job_id, group, r))
    if not membership:
        return ordered[:size]
    prefix = group.lower() + "_"
    set_of: dict[int, str] = {}
    for key, members in membership.items():
        if key.startswith(prefix):
            for r in members:
                set_of[r] = key
    by_set: dict[str, list[int]] = defaultdict(list)
    for r in ordered:
        by_set[set_of.get(r, "other")].append(r)
    chosen: list[int] = []
    for members in by_set.values():
        chosen.extend(members[:FLOOR_PER_SET])
    chosen = chosen[:size]
    taken = set(chosen)
    for r in ordered:
        if len(chosen) >= size:
            break
        if r not in taken:
            chosen.append(r)
            taken.add(r)
    return sorted(chosen, key=lambda r: _order_key(job_id, group, r))


def judge_request(item: dict[str, Any], profile: dict[str, Any] | None = None) -> JudgeRequest:
    """What the judge sees for one row. ``profile`` is the job's category profile, which says
    whether an ounce in this category is read as weight, fluid, or raised."""
    group = outcome_group(item)
    original = item.get("original") or {}
    context = item.get("context") or {}
    result = outcome(item)
    final = result["values"] if group == "B" else {}
    return JudgeRequest(
        question=QUESTION[group],
        descriptions={f: context.get(f) for f in (
            "item_brand_eng", "item_brand_local_lang", "item_desc_eng", "item_desc_local_lang",
            "web_description_eng", "web_description_chi")},
        category=context.get("category"), subcategory=context.get("subcategory"),
        category_kind=_ounce_kind(item, profile),
        legacy=Values(size=_s(original.get("legacy_size")), uom=original.get("legacy_uom")) if original.get("legacy_uom") else None,
        excel=Values(size=_s(original.get("standard_size")), uom=original.get("standard_uom"), pack_size=_s(original.get("standard_pack_size"))),
        tool=ToolAction(
            final=Values(size=final.get("standard_size"), uom=final.get("standard_uom"), pack_size=final.get("standard_pack_size")),
            label=result["status_label"], reason=result["comment"][:1200],
        ),
    )


def _ounce_kind(item: dict[str, Any], profile: dict[str, Any] | None) -> str:
    """How an ounce is read for this row: the tool's own finding when it made one (it reads
    the product's finest category level), else the category-level profile."""
    codes = {f.get("code") for f in item.get("findings") or []}
    if "OUNCE_MAY_BE_FLUID" in codes:
        return "MIXED"
    if "OUNCE_READ_AS_FLUID" in codes:
        return "LIQUID"
    return category_kind(item, profile)


def _s(value: object) -> str | None:
    return None if value in (None, "") else str(value)


def figure(verdicts: Iterable[str]) -> dict[str, Any]:
    counts = Counter(verdicts)
    right, wrong, unsure = counts["RIGHT"], counts["WRONG"], counts["CANT_TELL"]
    judged = right + wrong
    percent = round(100 * right / judged, 1) if judged else None
    # 95 % margin on a proportion; the sample is small, so it is shown beside the figure.
    margin = round(100 * 1.96 * sqrt((right / judged) * (1 - right / judged) / judged), 1) if judged else None
    return {"right": right, "wrong": wrong, "cant_tell": unsure, "judged": judged,
            "percent": percent if judged >= MIN_FOR_PERCENT else None, "margin": margin if judged >= MIN_FOR_PERCENT else None,
            "too_few": judged < MIN_FOR_PERCENT}


class SampleCheckService:
    def __init__(self, provider: Any, max_concurrency: int = 5, category_profile: dict[str, Any] | None = None):
        self.provider = provider
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.category_profile = category_profile

    async def judge_rows(self, items: list[dict[str, Any]], on_done: Callable[[int], None] = lambda n: None) -> list[dict[str, Any]]:
        done = 0
        results: list[dict[str, Any]] = [None] * len(items)  # type: ignore[list-item]

        async def one(index: int, item: dict[str, Any]) -> None:
            nonlocal done
            group = outcome_group(item)
            request = judge_request(item, self.category_profile)
            entry: dict[str, Any] = {"row_number": item["row_number"], "item_no": item.get("item_no"), "group": group,
                                     "tool_label": request.tool.label, "tool_reason": request.tool.reason[:300]}
            try:
                async with self.semaphore:
                    response = await self.provider.judge(request)
                r = response.result
                entry.update({"verdict": r.verdict, "verdict_label": VERDICT_WORDS[group][r.verdict], "basis": r.basis,
                              "reason": r.reason, "evidence": [e.model_dump() for e in r.evidence],
                              "model_id": response.model_id, "tokens": {"input": response.input_tokens, "output": response.output_tokens}})
            except Exception as exc:  # noqa: BLE001
                entry.update({"verdict": "CANT_TELL", "verdict_label": VERDICT_WORDS[group]["CANT_TELL"], "basis": "NONE",
                              "reason": f"The judge could not answer: {type(exc).__name__}", "error": str(exc)[:300], "evidence": []})
            results[index] = entry
            done += 1
            on_done(done)

        await asyncio.gather(*(one(i, item) for i, item in enumerate(items)))
        return results

    def run(self, job_id: str, sampled: dict[str, list[dict[str, Any]]], on_done: Callable[[int], None] = lambda n: None) -> dict[str, Any]:
        flat = [item for group in GROUPS for item in sampled.get(group, [])]
        rows = asyncio.run(self.judge_rows(flat, on_done))
        by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_group[row["group"]].append(row)
        groups = []
        for group in GROUPS:
            group_rows = by_group.get(group, [])
            groups.append({"group": group, "name": GROUP_NAMES[group], "question": QUESTION_TEXT[group],
                           "sample": len(group_rows), "population": sum(1 for _ in sampled.get(group, [])),
                           **figure(r["verdict"] for r in group_rows), "verdict_words": VERDICT_WORDS[group]})
        tokens = Counter()
        for row in rows:
            for k, v in (row.get("tokens") or {}).items():
                tokens[k] += v
        return {"job_id": job_id, "version": SAMPLE_CHECK_VERSION, "prompt_version": JUDGE_PROMPT_VERSION,
                "model_id": getattr(self.provider, "model_id", None), "judged_at": datetime.now(timezone.utc),
                "calls": len(rows), "tokens": dict(tokens), "groups": groups, "rows": rows}


def summarize_with_people(report: dict[str, Any], items_by_row: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """Add a person's verdicts (stored on the rows as ``verification``) beside the judge's,
    and the agreement between the two where both exist."""
    out = dict(report)
    groups = []
    for g in report.get("groups", []):
        rows = [r for r in report.get("rows", []) if r["group"] == g["group"]]
        human = []
        agree = both = 0
        for r in rows:
            v = ((items_by_row.get(int(r["row_number"])) or {}).get("verification") or {}).get("verdict")
            if v in ("CORRECT", "WRONG"):
                hv = "RIGHT" if v == "CORRECT" else "WRONG"
                human.append(hv)
                if r["verdict"] in ("RIGHT", "WRONG"):
                    both += 1
                    agree += hv == r["verdict"]
        groups.append({**g, "person": figure(human), "agreement": {"both": both, "agree": agree}})
    out["groups"] = groups
    return out
