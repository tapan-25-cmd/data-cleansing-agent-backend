from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.dependencies import repositories
from app.repositories.mongo import MongoRepositories
from app.services.rules_guide_service import build_guide

router = APIRouter(prefix="/rules", tags=["rules"])

# Documents from docs/ that the app shows as tabs. docs/ sits beside backend/ in the
# monorepo and beside app/ in the standalone backend repo.
DOCUMENTS = {"sequence": "pipeline-sequence.md", "accuracy-rules": "accuracy-rules.md"}
_HERE = Path(__file__).resolve()
DOCS_DIRS = (_HERE.parents[3] / "docs", _HERE.parents[2] / "docs")


def _document(name: str) -> dict:
    file_name = DOCUMENTS.get(name)
    if not file_name:
        raise HTTPException(404, "Unknown document")
    for folder in DOCS_DIRS:
        path = folder / file_name
        if path.is_file():
            return {"path": f"docs/{file_name}", "markdown": path.read_text(encoding="utf-8")}
    raise HTTPException(404, f"docs/{file_name} is not packaged with this server")


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
    return _document("sequence")


@router.get("/accuracy-rules")
def accuracy_rules() -> dict:
    """How accuracy is calculated (docs/accuracy-rules.md), for the Accuracy rules tab."""
    return _document("accuracy-rules")
