"""K L M blind test: 100 random rows per group, the right values worked out blind by a second
model, compared with ours by plain rules, with a Yes/No for a human reviewer on every row."""
from datetime import datetime, timezone
import secrets
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.dependencies import repositories
from app.api.sample_check import _job
from app.repositories.mongo import MongoRepositories
from app.services.klm_blind_test_service import DEFAULT_SAMPLES, DEFAULT_SIZE, QUESTION, KlmBlindTestService, draw_more, new_test, sample_numbers, summarize
from app.services.sample_check_service import GROUPS

router = APIRouter(prefix="/jobs", tags=["klm-blind-test"])


class RunRequest(BaseModel):
    """``samples`` separate random samples of ``size`` rows per group, sharing no row. Samples
    already checked for this run are kept and only the missing ones are drawn, from rows the
    kept ones did not take; ``fresh`` discards them and draws every sample again."""
    size: int = Field(default=DEFAULT_SIZE, ge=10, le=300)  # rows per group, per sample
    samples: int = Field(default=DEFAULT_SAMPLES, ge=1, le=6)
    fresh: bool = False


class Answer(BaseModel):
    answer: Literal["YES", "NO"] | None = None
    comment: str = Field(default="", max_length=600)


def _run(repos: MongoRepositories, checker, job_id: str, payload: RunRequest, concurrency: int, kept: dict | None) -> None:
    test: dict = {"job_id": job_id, "started_at": datetime.now(timezone.utc)}
    try:
        items = {g: repos.group_items(job_id, g) for g in GROUPS}
        populations = {g: len(v) for g, v in items.items()}
        seed = secrets.randbits(32)
        kept_rows = (kept or {}).get("rows") or []
        have = sample_numbers(kept) if kept else []
        seeds = dict((kept or {}).get("seeds") or ({"1": kept.get("seed")} if kept and have else {}))
        test = {**new_test(job_id, payload.size, seed, populations), "samples": payload.samples}
        # The draws are fixed before any row is checked, and never reuse a row of a kept sample.
        numbers = [n for n in range(1, payload.samples + 1) if n not in have]
        drawn = draw_more(items, payload.size, seed, len(numbers), {int(r["row_number"]) for r in kept_rows})
        for n in numbers:
            seeds[str(n)] = seed
        test["seeds"] = seeds
        total = sum(len(v) for sampled in drawn for v in sampled.values())

        def progress(done: int) -> None:
            if done % 10 == 0 or done == total:
                # The kept samples stay in the record while new ones are checked, so a stop loses nothing.
                repos.save_klm_blind_test({**test, "status": "RUNNING", "done": done, "total": total, "rows": kept_rows})

        progress(0)
        profile = ((repos.get_job(job_id) or {}).get("validation_policy") or {}).get("category_profile")
        rows = KlmBlindTestService(checker, concurrency, profile).run_samples(list(zip(numbers, drawn)), progress)
        for r in kept_rows:
            r.setdefault("sample", 1)
        repos.save_klm_blind_test({**test, "status": "READY", "rows": kept_rows + rows, "done": total, "total": total,
                                   "finished_at": datetime.now(timezone.utc)})
    except Exception as exc:  # noqa: BLE001
        if kept:  # a failed top-up never loses the samples already checked
            repos.save_klm_blind_test({**kept, "error": f"Adding samples failed: {type(exc).__name__}: {exc}"})
        else:
            repos.save_klm_blind_test({**test, "status": "FAILED", "error": f"{type(exc).__name__}: {exc}"})
        raise


@router.get("/{job_id}/klm-blind-test")
def klm_blind_test(job_id: str, request: Request, repos: Annotated[MongoRepositories, Depends(repositories)]) -> dict:
    _job(repos, job_id)
    base = {"questions": QUESTION, "default_size": DEFAULT_SIZE,
            "checker_model": getattr(getattr(request.app.state, "klm_checker", None), "model_id", None)}
    stored = repos.klm_blind_test(job_id)
    if not stored:
        return {"status": "NOT_RUN", **base}
    if stored.get("status") != "READY":
        return {**{k: v for k, v in stored.items() if k != "rows"}, **base}
    numbers = sample_numbers(stored)
    for r in stored.get("rows") or []:
        r.setdefault("sample", 1)
    return {**stored, "sample_numbers": numbers, "summary": summarize(stored),
            "sample_summaries": {str(n): summarize(stored, n) for n in numbers}, **base}


@router.post("/{job_id}/klm-blind-test/run", status_code=202)
def run_klm_blind_test(job_id: str, payload: RunRequest, request: Request, tasks: BackgroundTasks,
                       repos: Annotated[MongoRepositories, Depends(repositories)]) -> dict:
    _job(repos, job_id)
    checker = getattr(request.app.state, "klm_checker", None)
    if checker is None:
        raise HTTPException(409, "The checker is not configured on this server")
    stored = repos.klm_blind_test(job_id)
    if stored and stored.get("status") == "RUNNING":
        started = stored.get("started_at")
        if started and (datetime.now(timezone.utc) - started.replace(tzinfo=timezone.utc)).total_seconds() < 3600:
            raise HTTPException(409, "A K L M blind test is already running")
    kept = stored if (stored and stored.get("status") == "READY" and not payload.fresh
                      and stored.get("size") == payload.size and stored.get("rows")) else None
    if kept and len(sample_numbers(kept)) >= payload.samples:
        raise HTTPException(409, f"This run already has {len(sample_numbers(kept))} samples; ask for more, or a fresh draw")
    missing = payload.samples - (len(sample_numbers(kept)) if kept else 0)
    repos.save_klm_blind_test({**(kept or {"job_id": job_id}),
                               "job_id": job_id, "status": "RUNNING", "started_at": datetime.now(timezone.utc), "size": payload.size,
                               "done": 0, "total": payload.size * len(GROUPS) * missing})
    settings = request.app.state.settings
    tasks.add_task(_run, repos, checker, job_id, payload, settings.judge_concurrency or settings.ai_max_concurrency, kept)
    return {"status": "RUNNING", "size": payload.size, "samples": payload.samples, "new_samples": missing}


@router.put("/{job_id}/klm-blind-test/rows/{row_number}")
def answer_row(job_id: str, row_number: int, payload: Answer, repos: Annotated[MongoRepositories, Depends(repositories)]) -> dict:
    """The human reviewer's Yes/No (are our K L M right?) and comment for one row."""
    if not repos.set_klm_blind_answer(job_id, row_number, payload.answer, payload.comment.strip()):
        raise HTTPException(404, "Row is not in the K L M blind test")
    return {"ok": True}
