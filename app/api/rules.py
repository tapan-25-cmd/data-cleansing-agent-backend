from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.dependencies import repositories
from app.repositories.mongo import MongoRepositories
from app.services.rules_guide_service import build_guide

router = APIRouter(prefix="/rules", tags=["rules"])

SEQUENCE_DOC = "pipeline-sequence.md"
# docs/ sits beside backend/ in the monorepo and beside app/ in the standalone backend repo.
_HERE = Path(__file__).resolve()
DOCS_DIRS = (_HERE.parents[3] / "docs", _HERE.parents[2] / "docs")


@router.get("")
def rules_guide(
    request: Request, repos: Annotated[MongoRepositories, Depends(repositories)],
    job_id: str | None = None,
) -> dict:
    """The rules, tables and decisions the engine runs with, in plain language. With a
    job_id, also the units that workbook needed and the versions it ran with."""
    job = repos.get_job(job_id) if job_id else None
    return build_guide(request.app.state.registry, job)


@router.get("/sequence")
def pipeline_sequence() -> dict:
    """The pipeline sequence document (docs/pipeline-sequence.md), for the Sequence tab."""
    for folder in DOCS_DIRS:
        path = folder / SEQUENCE_DOC
        if path.is_file():
            return {"path": f"docs/{SEQUENCE_DOC}", "markdown": path.read_text(encoding="utf-8")}
    raise HTTPException(404, f"docs/{SEQUENCE_DOC} is not packaged with this server")
