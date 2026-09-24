from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.dependencies import repositories
from app.repositories.mongo import MongoRepositories
from app.services.rules_guide_service import build_guide

router = APIRouter(prefix="/rules", tags=["rules"])


@router.get("")
def rules_guide(
    request: Request, repos: Annotated[MongoRepositories, Depends(repositories)],
    job_id: str | None = None,
) -> dict:
    """The rules, tables and decisions the engine runs with, in plain language. With a
    job_id, also the units that workbook needed and the versions it ran with."""
    job = repos.get_job(job_id) if job_id else None
    return build_guide(request.app.state.registry, job)
