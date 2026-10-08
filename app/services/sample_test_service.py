"""Sample test: a fresh random sample per group (A 200, B 50, C 50 by default), each row
answered Yes/No with a reason by three independent sources: the retailer's website, the AI
judge, and a person. The result is stored per job and written as a sheet in the download."""
from __future__ import annotations

import asyncio
import random
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from app.services.job_comparison_service import outcome
from app.services.result_status import GROUP_NAMES, outcome_group
from app.services.sample_check_service import GROUPS, QUESTION_TEXT, SampleCheckService, overall, wilson
from app.services.web_check_service import DATA_MISLED, FOUND, NO, TOOL_MISSED, WellcomeClient, compare

SAMPLE_TEST_VERSION = "sample-test-v2"
DEFAULT_SIZES = {"A": 200, "B": 50, "C": 50}
# Web-first: how many online products to collect, and the most B and C may contribute so
# that the small groups get a real figure without taking the whole sample.
WEB_FIRST_TOTAL = 400
WEB_FIRST_CAPS = {"B": 100, "C": 100}
WEB_WORKERS = 4
_DESCRIPTION_FIELDS = ("item_desc_eng", "item_desc_local_lang", "web_description_eng", "web_description_chi")
SOURCES = ("web", "judge", "person")
_JUDGE_TO_YES = {"RIGHT": "YES", "WRONG": "NO"}


def draw(items_by_group: dict[str, list[dict[str, Any]]], sizes: dict[str, int], seed: int) -> dict[str, list[dict[str, Any]]]:
    rng = random.Random(seed)
    return {g: sorted(rng.sample(items_by_group[g], min(sizes[g], len(items_by_group[g]))), key=lambda x: int(x["row_number"]))
            for g in GROUPS}


def _v(values: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if values is None else {k: values.get(k) for k in ("standard_size", "standard_uom", "standard_pack_size")}


def base_row(item: dict[str, Any]) -> dict[str, Any]:
    group = outcome_group(item)
    result = outcome(item)
    context = item.get("context") or {}
    original = item.get("original") or {}
    return {
        "row_number": int(item["row_number"]), "item_no": str(item.get("item_no") or ""), "group": group,
        "product": context.get("item_desc_eng") or context.get("web_description_eng") or "",
        "product_local": context.get("item_desc_local_lang") or context.get("web_description_chi") or "",
        "category": context.get("category"),
        "legacy": " ".join(str(v) for v in (original.get("legacy_size"), original.get("legacy_uom")) if v not in (None, "")),
        "excel": _v(original), "final": _v(result["values"]), "suggestion": _v(result["suggestion"]),
        "action": result["status_label"],
    }


def figure(answers: list[str | None]) -> dict[str, Any]:
    yes, no = answers.count("YES"), answers.count("NO")
    not_comparable = answers.count("NOT_COMPARABLE")
    n = yes + no
    out = {"yes": yes, "no": no, "answered": n, "unanswered": len(answers) - n, "not_comparable": not_comparable,
           "percent": None, "low": None, "high": None}
    if n:
        low, high = wilson(yes / n, n)
        out.update(percent=round(100 * yes / n, 1), low=round(100 * low, 1), high=round(100 * high, 1))
    return out


def summarize(test: dict[str, Any]) -> dict[str, Any]:
    """Per group and overall (groups weighted by their share of the rows), for each source,
    and how often the website and the judge agree where both answered."""
    rows = test.get("rows") or []
    populations = test.get("populations") or {}
    out: dict[str, Any] = {"groups": [], "overall": {}, "agreement": {}}
    for source in SOURCES:
        per = []
        for g in GROUPS:
            f = figure([(r.get(source) or {}).get("answer") for r in rows if r["group"] == g])
            per.append({"group": g, "population": populations.get(g, 0), "judged": f["answered"], "cant_tell": 0, **f})
        out["groups"].append({"source": source, "per_group": per})
        ok = all(p["percent"] is not None for p in per)
        out["overall"][source] = overall(per) if ok else None
    both = [r for r in rows if (r.get("web") or {}).get("answer") in ("YES", "NO") and (r.get("judge") or {}).get("answer") in ("YES", "NO")]
    out["agreement"] = {"both": len(both), "agree": sum(r["web"]["answer"] == r["judge"]["answer"] for r in both)}
    out["web_found"] = sum((r.get("web") or {}).get("status") == FOUND for r in rows)
    kinds = Counter((r.get("web") or {}).get("kind") for r in rows if (r.get("web") or {}).get("answer") == NO)
    out["web_no"] = {"tool_missed": kinds.get(TOOL_MISSED, 0), "data_misled": kinds.get(DATA_MISLED, 0)}
    return out


class SampleTestService:
    def __init__(self, judge: Any, judge_concurrency: int = 6, category_profile: dict[str, Any] | None = None,
                 web: WellcomeClient | None = None):
        self.judge = judge
        self.judge_concurrency = judge_concurrency
        self.category_profile = category_profile
        self.web = web or WellcomeClient()

    def run(self, job_id: str, sampled: dict[str, list[dict[str, Any]]], on_progress: Callable[[str, int, int], None] = lambda s, d, t: None) -> list[dict[str, Any]]:
        flat = [item for g in GROUPS for item in sampled[g]]
        rows = {int(i["row_number"]): base_row(i) for i in flat}

        # The judge (async, parallel) and the website (one page at a time) run together.
        async def both() -> list[dict[str, Any]]:
            judging = SampleCheckService(self.judge, self.judge_concurrency, self.category_profile).judge_rows(
                flat, lambda d: on_progress("judge", d, len(flat)))
            web = asyncio.to_thread(self._web, flat, rows, on_progress)
            verdicts, _ = await asyncio.gather(judging, web)
            return verdicts

        for v in asyncio.run(both()):
            answer = _JUDGE_TO_YES.get(v["verdict"])
            rows[int(v["row_number"])]["judge"] = {"answer": answer, "verdict": v["verdict"], "reason": v.get("reason") or "",
                                                   "model_id": v.get("model_id")}
        for r in rows.values():
            r["person"] = {"answer": None, "reason": ""}
        return [rows[int(i["row_number"])] for i in flat]

    def _web(self, flat: list[dict[str, Any]], rows: dict[int, dict[str, Any]], on_progress: Callable[[str, int, int], None]) -> None:
        for n, item in enumerate(flat, 1):
            r = rows[int(item["row_number"])]
            site = self.web.lookup(r["item_no"])
            r["web"] = self._web_answer(item, r, site)
            on_progress("web", n, len(flat))

    @staticmethod
    def _web_answer(item: dict[str, Any], r: dict[str, Any], site: dict[str, Any]) -> dict[str, Any]:
        context = item.get("context") or {}
        answer = compare(r["group"], r["final"], r["suggestion"], r["excel"], site, [context.get(f) for f in _DESCRIPTION_FIELDS])
        return {**answer, **{k: site.get(k) for k in ("status", "url", "title", "spec", "checked_at")}}

    def run_web_first(self, job_id: str, items_by_group: dict[str, list[dict[str, Any]]], total: int, seed: int,
                      on_progress: Callable[[str, int, int], None] = lambda s, d, t: None,
                      per_group: int | None = None) -> list[dict[str, Any]]:
        """Look rows up on the website in random order, group by group, until ``total`` online
        products are collected (B and C capped so A fills the rest), or with ``per_group`` the
        same number from every group. Only those are judged."""
        rng = random.Random(seed)
        order = {g: rng.sample(v, len(v)) for g, v in items_by_group.items()}
        if per_group:
            caps = {g: per_group for g in GROUPS}
            total = per_group * len(GROUPS)
        else:
            caps = {"B": min(WEB_FIRST_CAPS["B"], total // 4), "C": min(WEB_FIRST_CAPS["C"], total // 4)}
        found: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {g: [] for g in GROUPS}
        scanned = 0
        with ThreadPoolExecutor(max_workers=WEB_WORKERS) as pool:
            for g in ("B", "C", "A"):
                want = caps.get(g) or total - sum(len(v) for v in found.values())
                queue = order[g]
                pos = 0
                while len(found[g]) < want and pos < len(queue):
                    batch = queue[pos: pos + WEB_WORKERS * 2]
                    pos += len(batch)
                    for item, site in zip(batch, pool.map(lambda it: self.web.lookup(str(it.get("item_no") or "")), batch)):
                        scanned += 1
                        if site.get("status") == FOUND and len(found[g]) < want:
                            found[g].append((item, site))
                    on_progress("web", sum(len(v) for v in found.values()), total)
                    on_progress("scanned", scanned, 0)
        flat = [item for g in GROUPS for item, _ in found[g]]
        rows = {int(i["row_number"]): base_row(i) for i in flat}
        for g in GROUPS:
            for item, site in found[g]:
                rows[int(item["row_number"])]["web"] = self._web_answer(item, rows[int(item["row_number"])], site)
        verdicts = asyncio.run(SampleCheckService(self.judge, self.judge_concurrency, self.category_profile).judge_rows(
            flat, lambda d: on_progress("judge", d, len(flat))))
        for v in verdicts:
            rows[int(v["row_number"])]["judge"] = {"answer": _JUDGE_TO_YES.get(v["verdict"]), "verdict": v["verdict"],
                                                   "reason": v.get("reason") or "", "model_id": v.get("model_id")}
        for r in rows.values():
            r["person"] = {"answer": None, "reason": ""}
        return [rows[int(i["row_number"])] for i in flat]


def new_test(job_id: str, sizes: dict[str, int], seed: int, populations: dict[str, int]) -> dict[str, Any]:
    return {"job_id": job_id, "version": SAMPLE_TEST_VERSION, "sizes": sizes, "seed": seed, "populations": populations,
            "groups_meta": [{"group": g, "name": GROUP_NAMES[g], "question": QUESTION_TEXT[g]} for g in GROUPS],
            "started_at": datetime.now(timezone.utc)}


def counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(r["group"] for r in rows))
