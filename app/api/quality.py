from typing import Annotated

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
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
from app.services.job_comparison_service import JobComparisonService

router = APIRouter(prefix="/jobs", tags=["quality"])
_READY = frozenset({
    "READY_FOR_REVIEW", "REVIEW_IN_PROGRESS", "READY_TO_EXPORT", "EXPORTING", "EXPORTED",
})


def _job_card(job: dict) -> dict:
    policy = job.get("validation_policy") or {}
    return {
        "job_id": job.get("job_id"),
        "created_at": job.get("created_at"),
        "status": job.get("status"),
        "file_name": job.get("original_file_name"),
        "engine": policy.get("version"),
        "guards": policy.get("guards_version"),
        "prompt": (job.get("ai_usage") or {}).get("prompt_version"),
    }


def _stamp(job: dict) -> str:
    return str(job.get("updated_at") or job.get("created_at") or "")


_BUILD_TIMEOUT = timedelta(minutes=20)


def build_job_comparison(repos: MongoRepositories, past: dict, job: dict) -> None:
    """Read both runs, compare every row, and store the result. Runs in the background
    because reading two full jobs from MongoDB Atlas takes minutes."""
    key = {"past_job_id": past["job_id"], "new_job_id": job["job_id"]}
    try:
        report = JobComparisonService().build(
            repos.comparison_items(past["job_id"]),
            repos.comparison_items(job["job_id"]),
        )
        rows = report.pop("rows")
        repos.replace_job_comparison_rows(past["job_id"], job["job_id"], rows)
        repos.save_job_comparison({
            **key, "status": "READY", "report": report,
            "past_updated_at": _stamp(past), "new_updated_at": _stamp(job),
            "built_at": datetime.now(timezone.utc),
        })
    except Exception as exc:  # noqa: BLE001 - the failure is shown on the page
        repos.save_job_comparison({
            **key, "status": "FAILED", "error": f"{type(exc).__name__}: {exc}",
            "past_updated_at": _stamp(past), "new_updated_at": _stamp(job),
        })
        raise


@router.get("/{job_id}/comparison")
def past_new_comparison(
    job_id: str,
    request: Request,
    tasks: BackgroundTasks,
    repos: Annotated[MongoRepositories, Depends(repositories)],
    baseline: str | None = None,
    change: str | None = None,
    past_status: str | None = None,
    new_status: str | None = None,
    group: str | None = None,
    search: str | None = None,
    changed_only: bool = True,
    page: int = 1,
    page_size: int = 50,
) -> Response:
    """Every row of this run beside the same row of an earlier run of the same workbook.

    The earlier run is ``baseline`` when given, otherwise the run this job was created
    to compare against, otherwise the most recent earlier finished run of the file.
    The first request starts the comparison and answers 202 until it is stored.
    """
    job = repos.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.get("status") not in _READY:
        raise HTTPException(409, "The workbook has not finished processing")
    baseline_id = baseline or (job.get("smoke_metadata") or {}).get("baseline_job_id")
    past = repos.get_job(baseline_id) if baseline_id else repos.previous_completed_job(job)
    if not past:
        raise HTTPException(
            409,
            "There is no earlier finished run of this workbook to compare with. "
            "Process the same file again after a change, then open this page on the new run.",
        )
    if past.get("status") not in _READY:
        raise HTTPException(409, "The earlier run has not finished processing")

    cards = {"past_job": _job_card(past), "new_job": _job_card(job)}
    stored = repos.job_comparison(past["job_id"], job["job_id"])
    fresh = bool(stored) and (
        stored.get("past_updated_at") == _stamp(past) and stored.get("new_updated_at") == _stamp(job)
    )
    building = bool(stored) and stored.get("status") == "BUILDING"
    if building:
        started = stored.get("started_at")
        if started and datetime.now(timezone.utc) - started.replace(tzinfo=timezone.utc) > _BUILD_TIMEOUT:
            building = False  # the earlier build never finished; start again
    if not building and (not stored or not fresh or stored.get("status") == "FAILED"):
        repos.save_job_comparison({
            "past_job_id": past["job_id"], "new_job_id": job["job_id"], "status": "BUILDING",
            "past_updated_at": _stamp(past), "new_updated_at": _stamp(job),
            "started_at": datetime.now(timezone.utc),
        })
        tasks.add_task(build_job_comparison, repos, past, job)
        building = True
    if building:
        return JSONResponse({"status": "BUILDING", **jsonable_encoder(cards)}, status_code=202)

    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    rows, total = repos.job_comparison_rows(
        past["job_id"], job["job_id"],
        {
            "change": change, "past_status": past_status, "new_status": new_status,
            "group": group, "search": search,
            "changed_only": changed_only and not change,
        },
        page, page_size,
    )
    report = dict(stored["report"])
    report.update({
        "status": "READY", **cards, "built_at": stored.get("built_at"),
        "rows": rows, "total": total, "page": page, "page_size": page_size,
    })
    return JSONResponse(jsonable_encoder(report))


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
