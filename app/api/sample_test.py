"""Sample test: A 200, B 50, C 50 random rows, each answered Yes/No with a reason by the
retailer's website, the AI judge and a person. Runs in the background."""
from datetime import datetime, timezone
import secrets
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.api.dependencies import repositories
from app.api.sample_check import _job
from app.repositories.mongo import MongoRepositories
from app.services.sample_check_service import GROUPS
from app.services.sample_test_service import DEFAULT_SIZES, WEB_FIRST_TOTAL, SampleTestService, draw, new_test, summarize
from app.services.sample_test_sheet import standalone_workbook

router = APIRouter(prefix="/jobs", tags=["sample-test"])


class RunRequest(BaseModel):
    """``web_first``: collect ``total`` products that are on the retailer's website and test
    those. ``per_group``: a fixed random sample per group, tested whether online or not."""
    mode: Literal["web_first", "per_group"] = "web_first"
    total: int = Field(default=WEB_FIRST_TOTAL, ge=50, le=1000)
    # web_first only: collect this many online products from every group instead of ``total``.
    per_group: int | None = Field(default=None, ge=10, le=300)
    sizes: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_SIZES))


class Answer(BaseModel):
    answer: Literal["YES", "NO"] | None = None
    reason: str = Field(default="", max_length=600)


def _total(payload: "RunRequest", sizes: dict[str, int]) -> int:
    if payload.mode != "web_first":
        return sum(sizes.values())
    return payload.per_group * len(GROUPS) if payload.per_group else payload.total


def _run(repos: MongoRepositories, judge, job_id: str, payload: "RunRequest", sizes: dict[str, int], concurrency: int) -> None:
    items = {g: repos.group_items(job_id, g) for g in GROUPS}
    populations = {g: len(v) for g, v in items.items()}
    seed = secrets.randbits(32)
    test = {**new_test(job_id, sizes, seed, populations), "mode": payload.mode, "total": _total(payload, sizes), "per_group": payload.per_group}
    progress = {"judge": 0, "web": 0, "scanned": 0}

    def on_progress(source: str, done: int, total: int) -> None:
        progress[source] = done
        if done % 10 == 0 or done == total or source == "scanned":
            repos.save_sample_test({**test, "status": "RUNNING", "progress": dict(progress)})

    try:
        profile = ((repos.get_job(job_id) or {}).get("validation_policy") or {}).get("category_profile")
        from app.services.web_check_service import WellcomeClient  # noqa: PLC0415
        service = SampleTestService(judge, concurrency, profile, web=WellcomeClient(store=repos))
        if payload.mode == "web_first":
            on_progress("web", 0, _total(payload, sizes))
            rows = service.run_web_first(job_id, items, payload.total, seed, on_progress, payload.per_group)
        else:
            sampled = draw(items, sizes, seed)
            on_progress("web", 0, sum(len(v) for v in sampled.values()))
            rows = service.run(job_id, sampled, on_progress)
        repos.save_sample_test({**test, "status": "READY", "rows": rows, "progress": dict(progress), "finished_at": datetime.now(timezone.utc)})
    except Exception as exc:  # noqa: BLE001
        repos.save_sample_test({**test, "status": "FAILED", "error": f"{type(exc).__name__}: {exc}"})
        raise


@router.get("/{job_id}/sample-test")
def sample_test(job_id: str, repos: Annotated[MongoRepositories, Depends(repositories)]) -> dict:
    _job(repos, job_id)
    stored = repos.sample_test(job_id)
    if not stored:
        return {"status": "NOT_RUN", "sizes": DEFAULT_SIZES}
    if stored.get("status") != "READY":
        return {k: v for k, v in stored.items() if k != "rows"}
    return {**stored, "summary": summarize(stored)}


@router.post("/{job_id}/sample-test/run", status_code=202)
def run_sample_test(job_id: str, payload: RunRequest, request: Request, tasks: BackgroundTasks,
                    repos: Annotated[MongoRepositories, Depends(repositories)]) -> dict:
    _job(repos, job_id)
    judge = getattr(request.app.state, "judge", None)
    if judge is None:
        raise HTTPException(409, "The judge is not configured on this server")
    stored = repos.sample_test(job_id)
    if stored and stored.get("status") == "RUNNING":
        started = stored.get("started_at")
        if started and (datetime.now(timezone.utc) - started.replace(tzinfo=timezone.utc)).total_seconds() < 3600:
            raise HTTPException(409, "A sample test is already running")
    sizes = {g: max(1, min(500, int(payload.sizes.get(g, DEFAULT_SIZES[g])))) for g in GROUPS}
    settings = request.app.state.settings
    total = _total(payload, sizes)
    repos.save_sample_test({"job_id": job_id, "status": "RUNNING", "started_at": datetime.now(timezone.utc), "sizes": sizes,
                            "mode": payload.mode, "progress": {"judge": 0, "web": 0, "scanned": 0}, "total": total})
    tasks.add_task(_run, repos, judge, job_id, payload, sizes, settings.judge_concurrency or settings.ai_max_concurrency)
    return {"status": "RUNNING", "mode": payload.mode, "total": total}


@router.get("/{job_id}/sample-test/download")
def download_sample_test(job_id: str, repos: Annotated[MongoRepositories, Depends(repositories)]) -> Response:
    """The sample test on its own, as a small workbook (one sheet)."""
    job = _job(repos, job_id)
    stored = repos.sample_test(job_id)
    if not stored or stored.get("status") != "READY":
        raise HTTPException(409, "The sample test has not finished")
    name = f"sample-test-{(job.get('original_file_name') or job_id).rsplit('.', 1)[0]}.xlsx"
    return Response(standalone_workbook(stored), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.put("/{job_id}/sample-test/rows/{row_number}")
def answer_row(job_id: str, row_number: int, payload: Answer, repos: Annotated[MongoRepositories, Depends(repositories)]) -> dict:
    if not repos.set_sample_test_answer(job_id, row_number, payload.answer, payload.reason.strip()):
        raise HTTPException(404, "Row is not in the sample test")
    return {"ok": True}
