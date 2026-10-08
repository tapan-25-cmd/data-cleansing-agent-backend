"""Sample check: judge a fixed random sample per group with a second model, one figure per
group. Runs in the background; each judged row is one paid model call on a real provider."""
from datetime import datetime, timezone
import secrets
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.dependencies import repositories
from app.repositories.mongo import MongoRepositories
from app.services.sample_check_service import DEFAULT_SIZES, GROUPS, QUESTION_TEXT, VERDICT_WORDS, SampleCheckService, draw_sample, pooled, random_draw, sample_key, strata, summarize_with_people
from app.services.result_status import GROUP_NAMES, group_query, outcome_group

router = APIRouter(prefix="/jobs", tags=["sample-check"])
_READY = frozenset({"READY_FOR_REVIEW", "REVIEW_IN_PROGRESS", "READY_TO_EXPORT", "EXPORTING", "EXPORTED"})


class RunRequest(BaseModel):
    """``random``: one fresh random draw of ``total`` rows from all live rows, each judged on
    its own group's question (the default). ``a_sample_bc_all``: ``total`` random rows from
    Group A and every row of Groups B and C, which are small enough to judge in full. ``per_group``: a fixed sample per group; ``size``
    sets all three at once."""
    mode: Literal["random", "a_sample_bc_all", "per_group"] = "random"
    total: int = Field(default=400, ge=20, le=2000)
    size: int | None = Field(default=None, ge=10, le=200)
    sizes: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_SIZES))

    def per_group(self) -> dict[str, int]:
        if self.size is not None:
            return {g: self.size for g in GROUPS}
        return {g: max(10, min(200, int(self.sizes.get(g, DEFAULT_SIZES[g])))) for g in GROUPS}


def _job(repos: MongoRepositories, job_id: str) -> dict:
    job = repos.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.get("status") not in _READY:
        raise HTTPException(409, "The workbook has not finished processing")
    return job


def _run_random(repos: MongoRepositories, provider, job_id: str, total: int, concurrency: int, bc_all: bool = False) -> None:
    started = datetime.now(timezone.utc)
    try:
        items = [x for group in GROUPS for x in repos.group_items(job_id, group)]
        populations = {g: sum(1 for x in items if outcome_group(x) == g) for g in GROUPS}
        seed = secrets.randbits(32)
        if bc_all:
            drawn = random_draw([x for x in items if outcome_group(x) == "A"], total, seed) + [x for x in items if outcome_group(x) in ("B", "C")]
        else:
            drawn = random_draw(items, total, seed)
        sampled = {g: [x for x in drawn if outcome_group(x) == g] for g in GROUPS}

        def progress(done: int) -> None:
            repos.save_sample_check({"job_id": job_id, "status": "RUNNING", "started_at": started, "done": done, "total": len(drawn)})

        progress(0)
        profile = ((repos.get_job(job_id) or {}).get("validation_policy") or {}).get("category_profile")
        report = SampleCheckService(provider, concurrency, profile).run(job_id, sampled, progress, None, populations)
        if not bc_all:  # one plain draw: every row counts once
            report["overall"] = pooled(report["rows"], sum(populations.values()))
        # With A sampled and B, C judged in full, the overall weights each group by its rows.
        mode = "a_sample_bc_all" if bc_all else "random"
        repos.save_sample_check({**report, "status": "READY", "draw": {"mode": mode, "total": len(drawn), "a_total": total, "seed": seed},
                                 "started_at": started, "finished_at": datetime.now(timezone.utc)})
    except Exception as exc:  # noqa: BLE001
        repos.save_sample_check({"job_id": job_id, "status": "FAILED", "error": f"{type(exc).__name__}: {exc}", "started_at": started})
        raise


def _run(repos: MongoRepositories, provider, job_id: str, sizes: dict[str, int], concurrency: int) -> None:
    started = datetime.now(timezone.utc)
    try:
        accuracy = repos.job_accuracy(job_id) or {}
        membership = (accuracy.get("report") or {}).get("membership") or {}
        sampled, strata_of, populations = {}, {}, {}
        key = sample_key(repos.get_job(job_id) or {"job_id": job_id})
        for group in GROUPS:
            items = repos.group_items(job_id, group)
            rows = set(draw_sample(key, items, group, sizes[group], membership))
            sampled[group] = [x for x in items if int(x["row_number"]) in rows]
            strata_of.update(strata(items, group, membership))
            populations[group] = len(items)
        total = sum(len(v) for v in sampled.values())

        def progress(done: int) -> None:
            repos.save_sample_check({"job_id": job_id, "status": "RUNNING", "started_at": started, "done": done, "total": total})

        progress(0)
        profile = ((repos.get_job(job_id) or {}).get("validation_policy") or {}).get("category_profile")
        report = SampleCheckService(provider, concurrency, profile).run(job_id, sampled, progress, strata_of, populations)
        repos.save_sample_check({**report, "status": "READY", "sizes": sizes, "draw": {"mode": "per_group", "sizes": sizes},
                                 "started_at": started, "finished_at": datetime.now(timezone.utc)})
    except Exception as exc:  # noqa: BLE001
        repos.save_sample_check({"job_id": job_id, "status": "FAILED", "error": f"{type(exc).__name__}: {exc}", "started_at": started})
        raise


@router.get("/{job_id}/sample-check")
def sample_check(job_id: str, request: Request, repos: Annotated[MongoRepositories, Depends(repositories)]) -> dict:
    _job(repos, job_id)
    stored = repos.sample_check(job_id)
    populations = {g: repos.db.job_items.count_documents({"job_id": job_id, **group_query(g)}) for g in GROUPS}
    base = {"groups_meta": [{"group": g, "name": GROUP_NAMES[g], "question": QUESTION_TEXT[g], "population": populations[g], "verdict_words": VERDICT_WORDS[g]} for g in GROUPS],
            "provider_is_real": getattr(request.app.state, "judge_is_real", False),
            "judge_model": getattr(getattr(request.app.state, "judge", None), "model_id", None)}
    if not stored:
        return {"status": "NOT_RUN", **base}
    if stored.get("status") != "READY":
        return {**{k: v for k, v in stored.items() if k != "rows"}, **base}
    rows = stored.get("rows") or []
    items = {int(x["row_number"]): x for x in repos.items_by_rows(job_id, [int(r["row_number"]) for r in rows])}
    report = summarize_with_people(stored, items)
    report.pop("rows", None)
    return {**report, **base}


@router.get("/{job_id}/sample-check/rows")
def sample_check_rows(job_id: str, repos: Annotated[MongoRepositories, Depends(repositories)],
                      group: Annotated[str, Query(pattern="^[ABC]$")]) -> dict:
    _job(repos, job_id)
    stored = repos.sample_check(job_id)
    if not stored or stored.get("status") != "READY":
        raise HTTPException(409, "The sample has not been judged yet")
    rows = [r for r in stored.get("rows") or [] if r["group"] == group]
    items = {int(x["row_number"]): x for x in repos.items_by_rows(job_id, [int(r["row_number"]) for r in rows])}
    out = []
    for r in rows:
        item = items.get(int(r["row_number"])) or {}
        context = item.get("context") or {}
        original = item.get("original") or {}
        person = (item.get("verification") or {}).get("verdict")
        out.append({**r, "product": context.get("item_desc_eng") or context.get("web_description_eng") or "",
                    "product_local": context.get("item_desc_local_lang") or context.get("web_description_chi") or "",
                    "descriptions": {k: context.get(k) for k in ("item_desc_eng", "item_desc_local_lang", "web_description_eng", "web_description_chi")},
                    "legacy": " ".join(str(v) for v in (original.get("legacy_size"), original.get("legacy_uom")) if v not in (None, "")),
                    "excel": {k: original.get(k) for k in ("standard_size", "standard_uom", "standard_pack_size")},
                    "tool": {"label": r.get("tool_label"), "reason": r.get("tool_reason")},
                    "person": {"CORRECT": "RIGHT", "WRONG": "WRONG"}.get(person)})
    return {"group": group, "rows": out}


@router.post("/{job_id}/sample-check/run", status_code=202)
def run_sample_check(job_id: str, payload: RunRequest, request: Request, tasks: BackgroundTasks,
                     repos: Annotated[MongoRepositories, Depends(repositories)]) -> dict:
    _job(repos, job_id)
    provider = getattr(request.app.state, "judge", None)
    if provider is None:
        raise HTTPException(409, "The judge is not configured on this server")
    stored = repos.sample_check(job_id)
    if stored and stored.get("status") == "RUNNING":
        started = stored.get("started_at")
        if started and (datetime.now(timezone.utc) - started.replace(tzinfo=timezone.utc)).total_seconds() < 1800:
            raise HTTPException(409, "A sample check is already running")
    repos.save_sample_check({"job_id": job_id, "status": "RUNNING", "started_at": datetime.now(timezone.utc), "done": 0, "total": 0})
    settings = request.app.state.settings
    concurrency = settings.judge_concurrency or settings.ai_max_concurrency
    if payload.mode in ("random", "a_sample_bc_all"):
        tasks.add_task(_run_random, repos, provider, job_id, payload.total, concurrency, payload.mode == "a_sample_bc_all")
        return {"status": "RUNNING", "mode": payload.mode, "total": payload.total}
    sizes = payload.per_group()
    tasks.add_task(_run, repos, provider, job_id, sizes, concurrency)
    return {"status": "RUNNING", "mode": "per_group", "sizes": sizes}
