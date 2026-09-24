from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.dependencies import repositories
from app.repositories.mongo import MongoRepositories
from app.services.open_questions_service import OpenQuestionsService, filter_rows, load_catalogue
from app.services.rule_engine import RuleEngine

router = APIRouter(prefix="/jobs", tags=["open-questions"])
_READY = frozenset({
    "READY_FOR_REVIEW", "REVIEW_IN_PROGRESS", "READY_TO_EXPORT", "EXPORTING", "EXPORTED",
})


class Answer(BaseModel):
    answer: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=2000)
    answered_by: str | None = Field(default=None, max_length=120)


def _report(request: Request, repos: MongoRepositories, job: dict) -> dict:
    """Built once per job version and kept in memory: only the rows that need
    attention are read, so this takes seconds, not minutes."""
    cache: dict = getattr(request.app.state, "open_questions_cache", None) or {}
    request.app.state.open_questions_cache = cache
    key = (job["job_id"], str(job.get("updated_at")))
    if key not in cache:
        cache.clear()
        service = OpenQuestionsService(RuleEngine(request.app.state.registry, 0))
        cache[key] = service.build(repos.attention_items(job["job_id"]), {})
    report = dict(cache[key])
    answers = repos.open_question_answers()
    report["groups"] = [
        {**group, "categories": [{**c, "answer": answers.get(c["id"])} for c in group["categories"]]}
        for group in report["groups"]
    ]
    report["answered"] = sum(1 for g in report["groups"] for c in g["categories"] if c["answer"])
    return report


def _job(repos: MongoRepositories, job_id: str) -> dict:
    job = repos.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.get("status") not in _READY:
        raise HTTPException(409, "The workbook has not finished processing")
    return job


@router.get("/{job_id}/open-questions")
def open_questions(
    job_id: str, request: Request, repos: Annotated[MongoRepositories, Depends(repositories)],
) -> dict:
    """Every problem category with its count, rule, question and recorded answer."""
    report = _report(request, repos, _job(repos, job_id))
    return {key: value for key, value in report.items() if key != "rows"}


@router.get("/{job_id}/open-questions/{category}")
def open_question_rows(
    job_id: str, category: str, request: Request,
    repos: Annotated[MongoRepositories, Depends(repositories)],
    search: str | None = None, group: str | None = None, status: str | None = None,
    page: int = 1, page_size: int = 50,
) -> dict:
    """All rows of one category, with descriptions, legacy, Excel, suggestion and options."""
    report = _report(request, repos, _job(repos, job_id))
    entry = next((c for g in report["groups"] for c in g["categories"] if c["id"] == category), None)
    if entry is None:
        raise HTTPException(404, "Unknown category")
    rows = filter_rows(report["rows"], category=category, search=search, group=group, status=status)
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    start = (page - 1) * page_size
    return {
        "category": entry, "rows": rows[start:start + page_size], "total": len(rows),
        "page": page, "page_size": page_size,
    }


@router.put("/{job_id}/open-questions/{category}/answer")
def answer_open_question(
    job_id: str, category: str, payload: Answer, request: Request,
    repos: Annotated[MongoRepositories, Depends(repositories)],
) -> dict:
    """Record the business answer for a category. It is shown on every workbook; it does
    not change any row, the export or the engine."""
    if category not in {c["id"] for c in load_catalogue()["categories"]}:
        raise HTTPException(404, "Unknown category")
    _job(repos, job_id)
    document = {
        "category": category, "answer": payload.answer, "note": payload.note,
        "answered_by": payload.answered_by, "answered_at": datetime.now(timezone.utc),
        "job_id": job_id,
    }
    repos.save_open_question_answer(document)
    return {"status": "RECORDED", "answer": document}
