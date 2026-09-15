from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile, status

from app.api.dependencies import processor, repositories, storage
from app.domain.enums import R1_DEPARTMENTS
from app.repositories.mongo import MongoRepositories
from app.services.excel_reader import ExcelReader, WorkbookValidationError
from app.services.processor import JobProcessor
from app.storage.local import LocalFileStorage, UploadTooLargeError

router = APIRouter(prefix="/jobs", tags=["jobs"])


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
    if job["status"] not in {"UPLOADED", "FAILED"}:
        raise HTTPException(409, f"Job cannot be processed from {job['status']}")
    repos.update_job(job_id, {
        "status": "VALIDATING",
        "ruleset_version": service.registry.version,
        "ruleset_checksum": service.registry.checksum,
        "progress.stage": "VALIDATING",
    })
    tasks.add_task(service.process, job_id)
    return {"job_id": job_id, "status": "VALIDATING"}


@router.get("/{job_id}")
def get_job(job_id: str, repos: Annotated[MongoRepositories, Depends(repositories)]) -> dict:
    job = repos.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
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


@router.get("/{job_id}/items")
def list_items(
    job_id: str,
    repos: Annotated[MongoRepositories, Depends(repositories)],
    group: str | None = None,
    discrepancy: bool | None = None,
    review_status: Literal["PENDING", "APPROVED", "REJECTED", "OVERRIDDEN"] | None = None,
    reason_code: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict:
    query: dict[str, object] = {}
    if group:
        query["group"] = group
    if discrepancy is not None:
        query["discrepancy.flagged"] = discrepancy
    if review_status is not None:
        query["review.overall_status"] = review_status
    if reason_code is not None:
        query["reason_code"] = reason_code
    rows, total = repos.list_items(job_id, query, (page - 1) * page_size, page_size)
    return {"items": rows, "total": total, "page": page, "page_size": page_size}


@router.get("/{job_id}/conversion-groups")
def conversion_groups(job_id: str, repos: Annotated[MongoRepositories, Depends(repositories)]) -> dict:
    if not repos.get_job(job_id):
        raise HTTPException(404, "Job not found")
    return {"groups": repos.conversion_groups(job_id)}
