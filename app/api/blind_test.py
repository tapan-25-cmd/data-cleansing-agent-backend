from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from app.api.dependencies import repositories
from app.repositories.mongo import MongoRepositories
from app.services.blind_test_service import (
    MUTATIONS, SilentTextTest, conversion_test, seeded_error_test, silent_candidates,
)
from app.services.group_a_validator import GroupAValidator
from app.services.rule_engine import RuleEngine

router = APIRouter(prefix="/jobs", tags=["blind-test"])
_READY = frozenset({"READY_FOR_REVIEW", "REVIEW_IN_PROGRESS", "READY_TO_EXPORT", "EXPORTING", "EXPORTED"})
KINDS = {"A", "B", "C_SILENT"}


def _job(repos: MongoRepositories, job_id: str) -> dict:
    job = repos.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.get("status") not in _READY:
        raise HTTPException(409, "The workbook has not finished processing")
    return job


def _run(repos: MongoRepositories, registry, provider, job_id: str, kind: str) -> None:
    started = datetime.now(timezone.utc)
    try:
        items = repos.quality_items(job_id)
        if kind == "B":
            report = conversion_test(items, RuleEngine(registry, 0))
        elif kind == "A":
            report = seeded_error_test(items, GroupAValidator(RuleEngine(registry, 0)))
        else:
            report = SilentTextTest(provider).run(silent_candidates(items))
        repos.save_blind_test({"job_id": job_id, **report, "status": "READY", "started_at": started, "finished_at": datetime.now(timezone.utc)})
    except Exception as exc:  # noqa: BLE001
        repos.save_blind_test({"job_id": job_id, "kind": kind, "status": "FAILED", "error": f"{type(exc).__name__}: {exc}", "started_at": started})
        raise


@router.get("/{job_id}/blind-test")
def blind_tests(job_id: str, repos: Annotated[MongoRepositories, Depends(repositories)]) -> dict:
    job = _job(repos, job_id)
    stored = repos.blind_tests(job_id)
    reading = job.get("ai_reading_test") or {}
    return {
        "mutations": MUTATIONS,
        "tests": {k: {kk: vv for kk, vv in v.items() if kk != "rows"} for k, v in stored.items()},
        "reading_test": {"status": reading.get("status"), "score": reading.get("score"), "prompt_version": reading.get("prompt_version"), "finished_at": reading.get("finished_at")} if reading else None,
    }


@router.get("/{job_id}/blind-test/{kind}/rows")
def blind_test_rows(job_id: str, kind: str, repos: Annotated[MongoRepositories, Depends(repositories)], page: int = 1, page_size: int = 50) -> dict:
    _job(repos, job_id)
    doc = repos.blind_tests(job_id).get(kind)
    if not doc or doc.get("status") != "READY":
        raise HTTPException(409, "This test has not been run yet")
    rows = doc.get("rows") or []
    page = max(1, page); page_size = max(1, min(page_size, 200))
    return {"kind": kind, "rows": rows[(page - 1) * page_size:page * page_size], "total": len(rows), "page": page, "page_size": page_size}


@router.post("/{job_id}/blind-test/{kind}/run", status_code=202)
def run_blind_test(job_id: str, kind: str, request: Request, tasks: BackgroundTasks,
                   repos: Annotated[MongoRepositories, Depends(repositories)]) -> dict:
    """A and B cost nothing and take seconds to a couple of minutes. C_SILENT makes one
    model call per product and needs the real AI provider."""
    _job(repos, job_id)
    if kind not in KINDS:
        raise HTTPException(404, "Unknown test")
    provider = getattr(request.app.state, "ai_reading_test", None)
    if kind == "C_SILENT":
        if not provider or not provider.provider_is_real:
            raise HTTPException(409, "The AI is switched off on this server (AI_PROVIDER=mock)")
        provider = provider.provider
    repos.save_blind_test({"job_id": job_id, "kind": kind, "status": "RUNNING", "started_at": datetime.now(timezone.utc)})
    tasks.add_task(_run, repos, request.app.state.registry, provider, job_id, kind)
    return {"status": "RUNNING", "kind": kind}
