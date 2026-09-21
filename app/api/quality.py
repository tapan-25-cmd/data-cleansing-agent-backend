from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.dependencies import repositories
from app.repositories.mongo import MongoRepositories
from app.services.ai_reading_test_service import AiReadingTestBlocked, AiReadingTestService
from app.services.quality_service import (
    QUALITY_REPORT_VERSION,
    RESULT_KINDS,
    QualityService,
    load_business_decisions,
)

router = APIRouter(prefix="/jobs", tags=["quality"])
_READY = frozenset({
    "READY_FOR_REVIEW", "REVIEW_IN_PROGRESS", "READY_TO_EXPORT", "EXPORTING", "EXPORTED",
})


@router.get("/{job_id}/quality")
def job_quality(
    job_id: str,
    request: Request,
    repos: Annotated[MongoRepositories, Depends(repositories)],
    refresh: bool = False,
) -> dict:
    job = repos.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.get("status") not in _READY:
        raise HTTPException(409, "The workbook has not finished processing")
    cached = job.get("quality")
    decisions_version, _ = load_business_decisions()
    if (
        not refresh
        and cached
        and cached.get("version") == QUALITY_REPORT_VERSION
        and cached.get("decisions_version") == decisions_version
    ):
        return cached
    # Jobs processed before the report existed, or whose review decisions changed,
    # are rebuilt from their stored result items. No model is called.
    report = QualityService(request.app.state.registry).build_report(
        job, repos.quality_items(job_id),
    )
    repos.update_job(job_id, {"quality": report})
    return report


@router.get("/{job_id}/verification-sample")
def verification_sample(
    job_id: str,
    kind: str,
    repos: Annotated[MongoRepositories, Depends(repositories)],
    size: int = 30,
) -> dict:
    if kind not in {key for key, *_ in RESULT_KINDS}:
        raise HTTPException(400, "Unknown kind of result")
    if not repos.get_job(job_id):
        raise HTTPException(404, "Job not found")
    return QualityService.verification_sample(
        job_id, repos.quality_items(job_id), kind, max(1, min(size, 200)),
    )


class AiReadingTestRequest(BaseModel):
    # None tests every eligible product. Each product is one paid model call.
    limit: int | None = Field(default=None, ge=1, le=5000)
    # Re-test only the products whose last reading gave no answer or failed.
    only_unanswered: bool = False


def _public(state: dict | None) -> dict:
    state = dict(state or {"status": "NOT_STARTED"})
    state.pop("score", None)
    return state


@router.get("/{job_id}/ai-reading-test")
def ai_reading_test_status(
    job_id: str,
    repos: Annotated[MongoRepositories, Depends(repositories)],
) -> dict:
    job = repos.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return _public(job.get("ai_reading_test"))


@router.post("/{job_id}/ai-reading-test", status_code=status.HTTP_202_ACCEPTED)
def start_ai_reading_test(
    job_id: str,
    payload: AiReadingTestRequest,
    request: Request,
    tasks: BackgroundTasks,
) -> dict:
    service: AiReadingTestService = request.app.state.ai_reading_test
    try:
        state, selected = service.start(
            job_id, payload.limit, only_unanswered=payload.only_unanswered,
        )
    except LookupError as exc:
        raise HTTPException(404, "Job not found") from exc
    except AiReadingTestBlocked as exc:
        raise HTTPException(409, str(exc)) from exc
    tasks.add_task(service.run, job_id, selected, only_unanswered=payload.only_unanswered)
    return _public(state)


@router.post("/{job_id}/ai-reading-test/rescore")
def rescore_ai_reading_test(job_id: str, request: Request) -> dict:
    """Apply the current conversion rules to the stored AI readings. No model call."""
    service: AiReadingTestService = request.app.state.ai_reading_test
    try:
        score = service.rescore(job_id)
    except LookupError as exc:
        raise HTTPException(404, "Job not found") from exc
    except AiReadingTestBlocked as exc:
        raise HTTPException(409, str(exc)) from exc
    return {key: score[key] for key in ("tested", "agreed", "no_answer", "disagreements")}
