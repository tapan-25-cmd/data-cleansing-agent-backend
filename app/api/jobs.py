from pathlib import Path
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile, status

from app.api.dependencies import processor, repositories, storage
from app.domain.enums import R1_DEPARTMENTS
from app.repositories.mongo import MongoRepositories
from app.services.excel_reader import ExcelReader, WorkbookValidationError
from app.services.export_service import EXPORT_VERSION
from app.services.processor import JobProcessor
from app.services.result_status import GROUPS, describe, group_query, status_query
from app.storage.local import LocalFileStorage, UploadTooLargeError

router = APIRouter(prefix="/jobs", tags=["jobs"])


def route_filter(route: str) -> dict[str, object]:
    """The method a row went through. Route A covers the rows the checker sent for review."""
    routes = ["A", "VALIDATION_REVIEW"] if route == "A" else [route]
    return {"route": {"$in": routes}}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_job(
    file: Annotated[UploadFile, File()],
    file_storage: Annotated[LocalFileStorage, Depends(storage)],
    repos: Annotated[MongoRepositories, Depends(repositories)],
    service: Annotated[JobProcessor, Depends(processor)],
    departments: Annotated[str | None, Form()] = None,
) -> dict[str, str]:
    filename = Path(file.filename or "").name
    if Path(filename).suffix.lower() != ".xlsx":
        raise HTTPException(400, "Only .xlsx workbooks are accepted")
    selected = {part.strip() for part in departments.split(",")} if departments else set(R1_DEPARTMENTS)
    if not selected or not selected.issubset(R1_DEPARTMENTS):
        raise HTTPException(400, f"Departments must be selected from {sorted(R1_DEPARTMENTS)}")
    job_id = str(uuid4())
    try:
        storage_key = file_storage.save_input(job_id, file.file)
        ExcelReader().validate(file_storage.get_input_path(job_id))
    except (UploadTooLargeError, WorkbookValidationError) as exc:
        raise HTTPException(422, str(exc)) from exc
    repos.create_job({
        "job_id": job_id,
        "original_file_name": filename,
        "input_storage_key": storage_key,
        "output_storage_key": None,
        "status": "UPLOADED",
        "selected_departments": sorted(selected),
        "snapshot_label": "v0.2" if "v0.2" in filename.lower() else None,
        "ruleset_version": service.registry.version,
        "ruleset_checksum": service.registry.checksum,
        "stats": {},
        "progress": {"stage": "UPLOADED", "processed": 0, "total": 0, "percent": 0},
        "error": None,
    })
    return {"job_id": job_id, "status": "UPLOADED"}


def require_storage_headroom(repos: MongoRepositories) -> None:
    headroom = repos.storage_headroom()
    if headroom is None:
        return
    used_mb, limit_mb = headroom
    if limit_mb - used_mb < MIN_FREE_MB:
        raise HTTPException(
            507,
            f"The results database is full: {used_mb:.0f} MB of {limit_mb:.0f} MB used. "
            "Delete workbooks you no longer need, or increase the database size, then try again.",
        )


@router.post("/{job_id}/process", status_code=status.HTTP_202_ACCEPTED)
def process_job(
    job_id: str,
    tasks: BackgroundTasks,
    service: Annotated[JobProcessor, Depends(processor)],
    repos: Annotated[MongoRepositories, Depends(repositories)],
) -> dict[str, str]:
    job = repos.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    require_storage_headroom(repos)
    job = mark_if_interrupted(job, repos)
    if job["status"] not in {"UPLOADED", "FAILED"}:
        raise HTTPException(409, f"Job cannot be processed from {job['status']}")
    # A restart begins from the workbook again; rows written by the interrupted run
    # would otherwise linger beside the new ones.
    repos.delete_items(job_id)
    repos.update_job(job_id, {
        "status": "VALIDATING",
        "ruleset_version": service.registry.version,
        "ruleset_checksum": service.registry.checksum,
        "progress.stage": "VALIDATING",
    })
    tasks.add_task(service.process, job_id)
    return {"job_id": job_id, "status": "VALIDATING"}


# Progress is written every few seconds while a job runs. A job that has reported
# nothing for this long is not running any more: the process that owned it was
# restarted (for example a dev server reload) and the background task died with it.
STALE_AFTER = timedelta(minutes=10)
PROCESSING_STATUSES = frozenset({
    "VALIDATING", "PROFILING", "PROCESSING", "PROCESSING_RULES",
    "PROCESSING_DESCRIPTIONS", "CHECKING_DISCREPANCIES",
})
INTERRUPTED_MESSAGE = (
    "Processing was interrupted before it finished: the server restarted or the database stopped "
    "accepting writes while the job was running. Nothing was lost from your upload: press "
    "Process again to run it from the start."
)
# A full run writes about 25 MB of results. Refuse to start one the database cannot hold,
# rather than letting it stop at 97 percent when the cluster's quota blocks writes.
MIN_FREE_MB = 40


def mark_if_interrupted(job: dict, repos: MongoRepositories) -> dict:
    """Turn a job that stopped reporting progress into a FAILED job the user can restart."""
    if job.get("status") not in PROCESSING_STATUSES:
        return job
    updated = job.get("updated_at")
    if updated is None:
        return job
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - updated < STALE_AFTER:
        return job
    repos.update_job(job["job_id"], {
        "status": "FAILED", "progress.stage": "FAILED", "error": INTERRUPTED_MESSAGE,
    })
    return {**job, "status": "FAILED", "error": INTERRUPTED_MESSAGE,
            "progress": {**(job.get("progress") or {}), "stage": "FAILED"}}


@router.get("/{job_id}")
def get_job(job_id: str, repos: Annotated[MongoRepositories, Depends(repositories)]) -> dict:
    job = repos.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job = mark_if_interrupted(job, repos)
    # A workbook built by an earlier exporter is offered for rebuilding rather than
    # handed over as-is, so a fix to the export reaches jobs already downloaded once.
    job["export_current"] = job.get("export_version") == EXPORT_VERSION
    return job


@router.get("/{job_id}/summary")
def get_summary(job_id: str, repos: Annotated[MongoRepositories, Depends(repositories)]) -> dict:
    job = repos.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {
        "job_id": job_id,
        "status": job["status"],
        "stats": job.get("stats", {}),
        "rule_readiness": job.get("rule_readiness", {}),
        "pending_review": repos.pending_count(job_id),
    }


@router.get("/{job_id}/results")
def list_results(
    job_id: str,
    repos: Annotated[MongoRepositories, Depends(repositories)],
    group: Literal["A", "B", "C", "PURGED"] | None = None,
    route: str | None = None,
    status: Literal[
        "NO_CHANGE", "AUTO_APPLY", "REVIEW_REQUIRED", "OBSERVATION_ONLY", "UNRESOLVED", "INVALID", "SKIPPED"
    ] | None = None,
    review_status: Literal[
        "NOT_REQUIRED", "PENDING", "APPROVED", "REJECTED", "OVERRIDDEN"
    ] | None = None,
    finding_category: str | None = None,
    finding_severity: str | None = None,
    changed_field: Literal[
        "standard_size", "standard_uom", "standard_pack_size"
    ] | None = None,
    search: Annotated[str | None, Query(max_length=100)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict:
    if not repos.get_job(job_id):
        raise HTTPException(404, "Job not found")
    # Each filter is its own clause: the group, status and search filters all use $or.
    clauses: list[dict[str, object]] = []
    if group:
        clauses.append(group_query(group))
    if route:
        clauses.append(route_filter(route))
    if status:
        clauses.append(status_query(status))
    if review_status:
        clauses.append({"review.overall_status": review_status})
    if finding_category:
        clauses.append({"findings.category": finding_category})
    if finding_severity:
        clauses.append({"findings.severity": finding_severity})
    if changed_field:
        clauses.append({"changes": {"$elemMatch": {
            "field": changed_field,
            "proposed": {"$ne": None},
        }}})
    if search and search.strip():
        safe = re.escape(search.strip())
        clauses.append({"$or": [
            {"item_no": {"$regex": safe, "$options": "i"}},
            {"context.item_desc_eng": {"$regex": safe, "$options": "i"}},
            {"context.web_description_eng": {"$regex": safe, "$options": "i"}},
        ]})
    query: dict[str, object] = {"$and": clauses} if clauses else {}
    rows, total = repos.list_items(
        job_id,
        query,
        (page - 1) * page_size,
        page_size,
    )
    for row in rows:
        describe(row)
    return {"items": rows, "total": total, "page": page, "page_size": page_size}


@router.get("/{job_id}/result-facets")
def result_facets(
    job_id: str,
    repos: Annotated[MongoRepositories, Depends(repositories)],
) -> dict:
    if not repos.get_job(job_id):
        raise HTTPException(404, "Job not found")
    return {"facets": repos.result_facets(job_id)}


@router.get("/{job_id}/items/{row_number}")
def get_result_item(
    job_id: str,
    row_number: int,
    repos: Annotated[MongoRepositories, Depends(repositories)],
) -> dict:
    item = repos.get_item(job_id, row_number)
    if not item:
        raise HTTPException(404, "Result item not found")
    return describe(item)


@router.get("/{job_id}/items")
def list_items(
    job_id: str,
    repos: Annotated[MongoRepositories, Depends(repositories)],
    group: Literal["A", "B", "C", "PURGED"] | None = None,
    route: str | None = None,
    discrepancy: bool | None = None,
    review_status: Literal["PENDING", "APPROVED", "REJECTED", "OVERRIDDEN"] | None = None,
    reason_code: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict:
    clauses: list[dict[str, object]] = []
    if group:
        clauses.append(group_query(group))
    if route:
        clauses.append(route_filter(route))
    if discrepancy is not None:
        clauses.append({"discrepancy.flagged": discrepancy})
    if review_status is not None:
        clauses.append({"review.overall_status": review_status})
    if reason_code is not None:
        clauses.append({"reason_code": reason_code})
    query: dict[str, object] = {"$and": clauses} if clauses else {}
    rows, total = repos.list_items(job_id, query, (page - 1) * page_size, page_size)
    return {"items": [describe(row) for row in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/{job_id}/preview")
def preview_items(
    job_id: str,
    repos: Annotated[MongoRepositories, Depends(repositories)],
    limit: Annotated[int, Query(ge=3, le=30)] = 9,
) -> dict:
    if not repos.get_job(job_id):
        raise HTTPException(404, "Job not found")
    per_group = max(1, limit // 3)
    items: list[dict] = []
    for group in GROUPS[:3]:
        rows, _ = repos.list_items(job_id, group_query(group), 0, per_group)
        items.extend(describe(row) for row in rows)
    return {"items": items[:limit]}


@router.get("/{job_id}/conversion-groups")
def conversion_groups(job_id: str, repos: Annotated[MongoRepositories, Depends(repositories)]) -> dict:
    if not repos.get_job(job_id):
        raise HTTPException(404, "Job not found")
    return {"groups": repos.conversion_groups(job_id)}
