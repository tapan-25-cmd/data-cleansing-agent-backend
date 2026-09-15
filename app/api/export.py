from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import FileResponse

from app.api.dependencies import exporter, repositories, storage
from app.repositories.mongo import MongoRepositories
from app.services.export_service import ExportBlockedError, ExportService
from app.storage.local import LocalFileStorage

router = APIRouter(prefix="/jobs", tags=["export"])


@router.post("/{job_id}/export", status_code=status.HTTP_202_ACCEPTED)
def export_job(
    job_id: str,
    tasks: BackgroundTasks,
    service: Annotated[ExportService, Depends(exporter)],
) -> dict[str, str]:
    try:
        service.prepare_export(job_id)
    except ExportBlockedError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    tasks.add_task(service.export_in_background, job_id)
    return {"job_id": job_id, "status": "EXPORTING"}


@router.get("/{job_id}/download")
def download_job(
    job_id: str,
    repos: Annotated[MongoRepositories, Depends(repositories)],
    file_storage: Annotated[LocalFileStorage, Depends(storage)],
) -> FileResponse:
    job = repos.get_job(job_id)
    path = file_storage.get_output_path(job_id)
    if not job or job.get("status") != "EXPORTED" or not path.exists():
        raise HTTPException(404, "Exported workbook not found")
    source_name = Path(job["original_file_name"]).stem
    return FileResponse(path, filename=f"{source_name}-cleansed.xlsx")
