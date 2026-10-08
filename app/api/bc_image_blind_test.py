"""B/C image-only blind test using persisted, answer-free pack readings."""
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from app.api.dependencies import repositories
from app.api.sample_check import _job
from app.repositories.mongo import MongoRepositories
from app.services.bc_image_blind_test_service import build

router = APIRouter(prefix="/jobs", tags=["bc-image-blind-test"])


def _run(repos: MongoRepositories, job_id: str) -> None:
    try:
        report = build(job_id, repos.web_evidence(job_id))
        repos.save_bc_image_blind_test(report)
    except Exception as exc:  # noqa: BLE001
        repos.db.bc_image_blind_runs.replace_one({"job_id": job_id}, {
            "job_id": job_id, "status": "FAILED", "error": f"{type(exc).__name__}: {exc}",
            "saved_at": datetime.now(timezone.utc),
        }, upsert=True)
        raise


@router.post("/{job_id}/bc-image-blind-test/run", status_code=202)
def run_test(job_id: str, tasks: BackgroundTasks,
             repos: Annotated[MongoRepositories, Depends(repositories)]) -> dict:
    _job(repos, job_id)
    docs = [d for d in repos.web_evidence(job_id) if d.get("group") in {"B", "C"} and d.get("status") == "FOUND"]
    if not docs:
        raise HTTPException(409, "No completed Wellcome image evidence exists for Group B or C")
    repos.db.bc_image_blind_runs.replace_one({"job_id": job_id}, {
        "job_id": job_id, "status": "RUNNING", "population": len(docs),
        "started_at": datetime.now(timezone.utc), "saved_at": datetime.now(timezone.utc),
    }, upsert=True)
    tasks.add_task(_run, repos, job_id)
    return {"status": "RUNNING", "population": len(docs)}


@router.get("/{job_id}/bc-image-blind-test")
def get_test(job_id: str, repos: Annotated[MongoRepositories, Depends(repositories)]) -> dict:
    _job(repos, job_id)
    return repos.bc_image_blind_test(job_id) or {"job_id": job_id, "status": "NOT_RUN"}


@router.get("/{job_id}/bc-image-blind-test/rows")
def get_rows(job_id: str, repos: Annotated[MongoRepositories, Depends(repositories)], page: int = 1,
             page_size: int = 100, group: str | None = None, verdict: str | None = None) -> dict:
    _job(repos, job_id)
    page, page_size = max(1, page), max(1, min(200, page_size))
    rows, total = repos.bc_image_blind_rows(job_id, (page - 1) * page_size, page_size, group, verdict)
    return {"rows": rows, "total": total, "page": page, "page_size": page_size}
