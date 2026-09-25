"""Sample check: judge a fixed random sample per group with a second model, one figure per
group. Runs in the background; each judged row is one paid model call on a real provider."""
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.dependencies import repositories
from app.repositories.mongo import MongoRepositories
from app.services.sample_check_service import GROUPS, QUESTION_TEXT, VERDICT_WORDS, SampleCheckService, draw_sample, summarize_with_people
from app.services.result_status import GROUP_NAMES, group_query

router = APIRouter(prefix="/jobs", tags=["sample-check"])
_READY = frozenset({"READY_FOR_REVIEW", "REVIEW_IN_PROGRESS", "READY_TO_EXPORT", "EXPORTING", "EXPORTED"})


class RunRequest(BaseModel):
    size: int = Field(default=30, ge=10, le=100)


def _job(repos: MongoRepositories, job_id: str) -> dict:
    job = repos.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.get("status") not in _READY:
        raise HTTPException(409, "The workbook has not finished processing")
    return job


def _run(repos: MongoRepositories, provider, job_id: str, size: int, concurrency: int) -> None:
    started = datetime.now(timezone.utc)
    try:
        accuracy = repos.job_accuracy(job_id) or {}
        membership = (accuracy.get("report") or {}).get("membership") or {}
        sampled = {}
        for group in GROUPS:
            items = repos.group_items(job_id, group)
            rows = set(draw_sample(job_id, items, group, size, membership))
            sampled[group] = [x for x in items if int(x["row_number"]) in rows]
        total = sum(len(v) for v in sampled.values())

        def progress(done: int) -> None:
            repos.save_sample_check({"job_id": job_id, "status": "RUNNING", "started_at": started, "done": done, "total": total})

        progress(0)
        report = SampleCheckService(provider, concurrency).run(job_id, sampled, progress)
        repos.save_sample_check({**report, "status": "READY", "size": size, "started_at": started, "finished_at": datetime.now(timezone.utc)})
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
    tasks.add_task(_run, repos, provider, job_id, payload.size, request.app.state.settings.ai_max_concurrency)
    return {"status": "RUNNING", "size": payload.size}
