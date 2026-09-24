from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import repositories
from app.repositories.mongo import MongoRepositories

router = APIRouter(prefix="/jobs", tags=["reasoning"])


@router.get("/{job_id}/reasoning")
def reasoning_summary(job_id: str, repos: Annotated[MongoRepositories, Depends(repositories)]) -> dict:
    """The latest Lane A reasoning trial for this job: summary and per-category counts.
    Shadow only: nothing here has changed a row."""
    if not repos.get_job(job_id):
        raise HTTPException(404, "Job not found")
    latest = repos.latest_lane_a_trial(job_id)
    if not latest:
        return {"status": "NOT_RUN"}
    return {"status": "READY", **{k: v for k, v in latest.items() if k != "rows"}}


@router.get("/{job_id}/reasoning/{row_number}")
def reasoning_row(job_id: str, row_number: int, repos: Annotated[MongoRepositories, Depends(repositories)]) -> dict:
    if not repos.get_job(job_id):
        raise HTTPException(404, "Job not found")
    row = repos.lane_a_trial_row(job_id, row_number)
    return {"status": "READY", "row": row} if row else {"status": "NOT_RUN", "row": None}
