"""Sample check: a fixed random sample per outcome group, judged by a second model (or a
person) on the group's one question, giving one figure per group and one for all groups.

The draw is repeatable (hash of job, group and row) so nobody can cherry-pick and later
verdicts land on the same rows. Inside a group the draw is stratified over our accuracy
sets, so small corners (fluid ounces, spelling fixes) are seen: a floor per set, the rest at
random. Because the floor over-represents small sets, each judged row is weighted by its
set's size ÷ the rows sampled from that set, so the group figure is an estimate for the whole
group. The figure is right ÷ (right + wrong), CANT_TELL beside it, with a 95 % Wilson range on
the effective sample size. The all-groups figure weights each group by its share of the live
rows; its range is the same weighting of the group ranges (conservative).
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

SAMPLE_CHECK_VERSION = "sample-check-v3"
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
DEFAULT_SIZES = {"A": 100, "B": 30, "C": 30}
Z = 1.96


def _order_key(sample_key: str, group: str, row: int) -> str:
    return sha256(f"{sample_key}:{group}:{row}".encode()).hexdigest()


def sample_key(job: dict[str, Any]) -> str:
    """Runs of the same workbook share one sample order, so a rerun is judged on the same
    products and before/after figures compare like with like."""
    return str(job.get("original_file_name") or job.get("job_id") or "")


def strata(items: Iterable[dict[str, Any]], group: str, membership: dict[str, list[int]] | None) -> dict[int, tuple[str, int]]:
    """For every row of the group: its accuracy set and that set's size in the group."""
    rows = [int(x["row_number"]) for x in items if outcome_group(x) == group]
    set_of: dict[int, str] = {}
    prefix = group.lower() + "_"
    for key, members in (membership or {}).items():
        if key.startswith(prefix):
            for r in members:
                set_of[r] = key
    sizes = Counter(set_of.get(r, "other") for r in rows)
    return {r: (set_of.get(r, "other"), sizes[set_of.get(r, "other")]) for r in rows}


def draw_sample(key: str, items: Iterable[dict[str, Any]], group: str, size: int,
                membership: dict[str, list[int]] | None = None) -> list[int]:
    """Row numbers of the sample for one group: a floor from each of our sets, the rest by
    the fixed random order. Without a membership map the draw is plain random."""
    rows = [int(x["row_number"]) for x in items if outcome_group(x) == group]
    ordered = sorted(rows, key=lambda r: _order_key(key, group, r))
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
    return sorted(chosen, key=lambda r: _order_key(key, group, r))


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
    """How an ounce is read for this row, as the tool read it. The tool decides from the
    product's finest category level and records a finding when that level is mixed or liquid;
    no finding means it read a weight. The category-level profile is used only for rows that
    carry no ounce, where it is context and decides nothing."""
    codes = {f.get("code") for f in item.get("findings") or []}
    if "OUNCE_MAY_BE_FLUID" in codes:
        return "MIXED"
    if "OUNCE_READ_AS_FLUID" in codes:
        return "LIQUID"
    original = item.get("original") or {}
    units = {str(original.get(k) or "").strip().upper() for k in ("legacy_uom", "standard_uom")}
    if units & {"OZ", "FZ", "FLOZ", "FL OZ"}:
        return "UNKNOWN"
    return category_kind(item, profile)


def _s(value: object) -> str | None:
    return None if value in (None, "") else str(value)


def wilson(p: float, n: float) -> tuple[float, float]:
    """95 % Wilson range for a proportion; unlike the plain ± formula it stays honest at 0 % and
    100 % (10 of 10 right means "at least 72 %", not "100 % ± 0")."""
    if n <= 0:
        return 0.0, 1.0
    denominator = 1 + Z * Z / n
    centre = (p + Z * Z / (2 * n)) / denominator
    half = Z * sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def figure(verdicts: Iterable[str], weights: Iterable[float] | None = None) -> dict[str, Any]:
    """Right ÷ (right + wrong) with its range. With ``weights`` (one per verdict) the figure is
    the weighted estimate and the range uses the effective sample size."""
    verdicts = list(verdicts)
    weights = list(weights) if weights is not None else [1.0] * len(verdicts)
    counts = Counter(verdicts)
    right, wrong, unsure = counts["RIGHT"], counts["WRONG"], counts["CANT_TELL"]
    judged = right + wrong
    w_judged = [w for v, w in zip(verdicts, weights) if v in ("RIGHT", "WRONG")]
    w_right = sum(w for v, w in zip(verdicts, weights) if v == "RIGHT")
    total = sum(w_judged)
    p = w_right / total if total else None
    n_eff = (total * total / sum(w * w for w in w_judged)) if w_judged else 0.0
    low, high = wilson(p, n_eff) if p is not None else (None, None)
    enough = judged >= MIN_FOR_PERCENT
    return {"right": right, "wrong": wrong, "cant_tell": unsure, "judged": judged,
            "percent": round(100 * p, 1) if p is not None and enough else None,
            "low": round(100 * low, 1) if low is not None and enough else None,
            "high": round(100 * high, 1) if high is not None and enough else None,
            "raw_percent": round(100 * right / judged, 1) if judged else None,
            "effective_n": round(n_eff, 1), "too_few": not enough}


def random_draw(items: list[dict[str, Any]], total: int, seed: int) -> list[dict[str, Any]]:
    """A plain random draw from all live rows, whatever their group. Each run takes a new seed,
    which is saved so the draw can be repeated exactly."""
    import random
    live = [x for x in items if outcome_group(x) in GROUPS]
    return random.Random(seed).sample(live, min(total, len(live)))


def pooled(rows: list[dict[str, Any]], population: int) -> dict[str, Any]:
    """The figure for a plain random draw: every row counts once, as drawn. Groups appear in
    their natural share, so no weighting is needed."""
    f = figure([r["verdict"] for r in rows])
    return {"population": population, "percent": f["percent"], "low": f["low"], "high": f["high"],
            "judged": f["judged"], "cant_tell": f["cant_tell"], "right": f["right"], "wrong": f["wrong"]}


def overall(groups: list[dict[str, Any]]) -> dict[str, Any] | None:
    """All live rows: each group's figure weighted by its share of the rows. Shown only when
    every group has a figure."""
    if not groups or any(g.get("percent") is None for g in groups):
        return None
    population = sum(g["population"] for g in groups)
    if not population:
        return None
    share = {g["group"]: g["population"] / population for g in groups}
    value = lambda key: round(sum(share[g["group"]] * g[key] for g in groups), 1)  # noqa: E731
    return {"population": population, "percent": value("percent"), "low": value("low"), "high": value("high"),
            "weights": {k: round(100 * v, 1) for k, v in share.items()},
            "judged": sum(g["judged"] for g in groups), "cant_tell": sum(g["cant_tell"] for g in groups)}


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

    def run(self, job_id: str, sampled: dict[str, list[dict[str, Any]]], on_done: Callable[[int], None] = lambda n: None,
            strata_of: dict[int, tuple[str, int]] | None = None, populations: dict[str, int] | None = None) -> dict[str, Any]:
        """``strata_of`` maps a row to (its accuracy set, that set's size in the group), and
        ``populations`` gives each group's row count; without them every row weighs the same."""
        flat = [item for group in GROUPS for item in sampled.get(group, [])]
        rows = asyncio.run(self.judge_rows(flat, on_done))
        strata_of = strata_of or {}
        sampled_per_set = Counter(strata_of.get(int(r["row_number"]), ("other", 1))[0] for r in rows)
        for row in rows:
            name, size = strata_of.get(int(row["row_number"]), ("other", 1))
            row["stratum"], row["weight"] = name, (size / sampled_per_set[name]) if strata_of else 1.0
        by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_group[row["group"]].append(row)
        groups = []
        for group in GROUPS:
            group_rows = by_group.get(group, [])
            groups.append({"group": group, "name": GROUP_NAMES[group], "question": QUESTION_TEXT[group],
                           "sample": len(group_rows), "population": (populations or {}).get(group, len(group_rows)),
                           **figure([r["verdict"] for r in group_rows], [r["weight"] for r in group_rows]),
                           "verdict_words": VERDICT_WORDS[group]})
        tokens = Counter()
        for row in rows:
            for k, v in (row.get("tokens") or {}).items():
                tokens[k] += v
        return {"job_id": job_id, "version": SAMPLE_CHECK_VERSION, "prompt_version": JUDGE_PROMPT_VERSION,
                "model_id": getattr(self.provider, "model_id", None), "judged_at": datetime.now(timezone.utc),
                "calls": len(rows), "tokens": dict(tokens), "groups": groups, "overall": overall(groups), "rows": rows}


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
