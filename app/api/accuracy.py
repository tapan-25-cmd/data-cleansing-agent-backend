from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.api.dependencies import repositories
from app.repositories.mongo import MongoRepositories
from app.services.accuracy_service import ACCURACY_VERSION, SETS, AccuracyService
from app.services.job_comparison_service import outcome
from app.services.rule_engine import RuleEngine
from app.services.result_status import outcome_group, route_of

router = APIRouter(prefix="/jobs", tags=["accuracy"])
_READY = frozenset({"READY_FOR_REVIEW", "REVIEW_IN_PROGRESS", "READY_TO_EXPORT", "EXPORTING", "EXPORTED"})
_BUILD_TIMEOUT = timedelta(minutes=15)


def _stamp(job: dict, trial: dict | None) -> str:
    return f"{job.get('updated_at')}|{(trial or {}).get('run_id')}|{ACCURACY_VERSION}"


def build_accuracy(repos: MongoRepositories, registry, job: dict, previous: dict | None) -> None:
    trial = repos.latest_lane_a_trial(job["job_id"])
    rows = repos.lane_a_trial_rows(job["job_id"], trial["run_id"]) if trial else []
    try:
        report = AccuracyService(RuleEngine(registry, 0)).build(repos.accuracy_items(job["job_id"]), rows)
        repos.save_job_accuracy({"job_id": job["job_id"], "status": "READY", "stamp": _stamp(job, trial),
                                 "report": report, "built_at": datetime.now(timezone.utc)})
    except Exception as exc:  # noqa: BLE001
        # Keep the last good report readable beside the error; never loop on a rebuild.
        repos.save_job_accuracy({"job_id": job["job_id"], "status": "FAILED", "error": f"{type(exc).__name__}: {exc}",
                                 "stamp": _stamp(job, trial), **({"report": previous["report"], "built_at": previous.get("built_at")} if previous and previous.get("report") else {})})
        raise


def _job(repos: MongoRepositories, job_id: str) -> dict:
    job = repos.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.get("status") not in _READY:
        raise HTTPException(409, "The workbook has not finished processing")
    return job


@router.get("/{job_id}/accuracy")
def accuracy(job_id: str, request: Request, tasks: BackgroundTasks,
             repos: Annotated[MongoRepositories, Depends(repositories)], rebuild: bool = False) -> Response:
    """Accuracy per group with every product placed in a named set. Built once per job
    version in the background; answers 202 while building."""
    job = _job(repos, job_id)
    trial = repos.latest_lane_a_trial(job_id)
    stored = repos.job_accuracy(job_id)
    stamp = _stamp(job, trial)
    fresh = bool(stored) and stored.get("stamp") == stamp
    building = bool(stored) and stored.get("status") == "BUILDING"
    if building:
        started = stored.get("started_at")
        if started and datetime.now(timezone.utc) - started.replace(tzinfo=timezone.utc) > _BUILD_TIMEOUT:
            building = False
    failed_for_this_stamp = bool(stored) and stored.get("status") == "FAILED" and fresh and not rebuild
    if not building and not failed_for_this_stamp and (not stored or not fresh or rebuild):
        previous = stored if stored and stored.get("report") else None
        repos.save_job_accuracy({"job_id": job_id, "status": "BUILDING", "stamp": stamp, "started_at": datetime.now(timezone.utc),
                                 **({"report": previous["report"], "built_at": previous.get("built_at")} if previous else {})})
        tasks.add_task(build_accuracy, repos, request.app.state.registry, job, previous)
        building = True
    if failed_for_this_stamp:
        return JSONResponse({"status": "FAILED", "error": stored.get("error")}, status_code=500)
    if building:
        # While rebuilding, the previous report stays readable so the page never goes blank.
        if stored and stored.get("report"):
            report = dict(stored["report"]); report.pop("membership", None); report.pop("witness", None)
            return JSONResponse(jsonable_encoder({"status": "READY", "rebuilding": True, "built_at": stored.get("built_at"), **report}))
        return JSONResponse({"status": "BUILDING"}, status_code=202)
    report = dict(stored["report"])
    report.pop("membership", None); report.pop("witness", None)
    return JSONResponse(jsonable_encoder({"status": "READY", "built_at": stored.get("built_at"),
                                          "reading_test": repos.latest_reading_test(job.get("original_file_name")), **report}))


@router.get("/{job_id}/accuracy/{set_id}")
def accuracy_set(job_id: str, set_id: str, repos: Annotated[MongoRepositories, Depends(repositories)],
                 page: int = 1, page_size: int = 50) -> dict:
    """The products behind one set, with their result and comment."""
    _job(repos, job_id)
    if set_id not in {s["id"] for sets in SETS.values() for s in sets}:
        raise HTTPException(404, "Unknown set")
    stored = repos.job_accuracy(job_id)
    if not stored or not stored.get("report"):
        raise HTTPException(409, "Accuracy is not built yet")
    witness = stored["report"].get("witness") or {}
    rows = list(stored["report"].get("membership", {}).get(set_id) or [])
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    chunk = rows[(page - 1) * page_size:page * page_size]
    items = repos.items_by_rows(job_id, chunk)
    out = []
    for item in items:
        result = outcome(item)
        context = item.get("context") or {}
        original = item.get("original") or {}
        out.append({
            "row_number": item["row_number"], "item_no": item.get("item_no"), "route": route_of(item), "group": outcome_group(item),
            "product": context.get("item_desc_eng") or context.get("web_description_eng") or "",
            "product_local": context.get("item_desc_local_lang") or context.get("web_description_chi") or "",
            "legacy": " ".join(str(v) for v in (original.get("legacy_size"), original.get("legacy_uom")) if v not in (None, "")),
            "status": result["status"], "status_label": result["status_label"], "values": result["values"],
            "suggestion": result["suggestion"], "comment": result["comment"],
            "witness": witness.get(str(item["row_number"]), ""),
        })
    return {"set_id": set_id, "rows": out, "total": len(rows), "page": page, "page_size": page_size,
            "missing": len(chunk) - len(items)}
