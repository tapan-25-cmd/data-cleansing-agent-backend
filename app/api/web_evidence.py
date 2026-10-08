"""Website evidence per item: page text and pack photo against our value, with sources."""
from datetime import datetime, timezone
import random
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.dependencies import repositories, storage
from app.api.sample_check import _job
from app.repositories.mongo import MongoRepositories
from app.services.excel_reader import INPUT_SHEET
from app.services.web_evidence_service import WebEvidenceService, evaluate
from app.storage.local import LocalFileStorage

router = APIRouter(prefix="/jobs", tags=["web-evidence"])

class RunRequest(BaseModel):
    count: int = Field(default=5, ge=1, le=500)
    item_nos: list[str] | None = None
    refresh: bool = False  # read the product pages again instead of reusing stored lookups


def _run(repos: MongoRepositories, reader, job_id: str, items: list[dict], sites: dict, refresh: bool = False) -> None:
    started = datetime.now(timezone.utc)
    repos.save_web_evidence_run({"job_id": job_id, "status": "RUNNING", "started_at": started, "done": 0, "total": len(items)})
    try:
        def on_done(n: int, doc: dict) -> None:
            repos.save_web_evidence(job_id, doc)
            repos.save_web_evidence_run({"job_id": job_id, "status": "RUNNING", "started_at": started, "done": n, "total": len(items)})
        from app.services.web_check_service import WellcomeClient  # noqa: PLC0415
        web = WellcomeClient(store=repos, reuse_days=0 if refresh else 30)
        WebEvidenceService(reader, web=web, store=repos).run(items, sites, on_done)
        repos.save_web_evidence_run({"job_id": job_id, "status": "READY", "started_at": started, "done": len(items), "total": len(items),
                                     "finished_at": datetime.now(timezone.utc)})
    except Exception as exc:  # noqa: BLE001
        repos.save_web_evidence_run({"job_id": job_id, "status": "FAILED", "error": f"{type(exc).__name__}: {exc}", "started_at": started})
        raise


@router.post("/{job_id}/web-evidence/run", status_code=202)
def run_web_evidence(job_id: str, payload: RunRequest, request: Request, tasks: BackgroundTasks,
                     repos: Annotated[MongoRepositories, Depends(repositories)]) -> dict:
    """Check ``count`` products that the last sample test found online (or the given item
    numbers), reusing the pages it already fetched."""
    _job(repos, job_id)
    reader = getattr(request.app.state, "pack_reader", None)
    if reader is None:
        raise HTTPException(409, "The pack reader is not configured on this server")
    # Products known to be online: this job's sample test, plus every product any earlier run
    # found (the store keeps lookups by item number).
    test = repos.sample_test(job_id) or {}
    found = {r["item_no"] for r in test.get("rows") or [] if (r.get("web") or {}).get("status") == "FOUND"}
    found |= set(repos.found_online())
    if payload.item_nos:
        chosen = payload.item_nos
    else:
        if not found:
            raise HTTPException(409, "No product is known to be online yet: run a sample test first")
        done = {d["item_no"] for d in repos.web_evidence(job_id) if d.get("status") == "FOUND"}
        pool = sorted(n for n in found if n not in done) or sorted(found)
        chosen = random.sample(pool, min(payload.count, len(pool)))
    items = {i["item_no"]: i for i in repos.items_by_item_nos(job_id, chosen)}
    ordered = [items[n] for n in chosen if n in items]
    if not ordered:
        raise HTTPException(404, "No such items in this run")
    # The sample test stored the page link and text, not the image links, so pages are re-read.
    tasks.add_task(_run, repos, reader, job_id, ordered, {}, payload.refresh)
    return {"status": "RUNNING", "items": [i["item_no"] for i in ordered]}


# The uploaded sheet's cells for the checked rows, read once per job and kept: the table shows
# each row exactly as it sits in the workbook, column letters included.
_SHEET_CACHE: dict[str, tuple[list[str], dict[int, list]]] = {}


def _column_letter(index: int) -> str:
    s = ""
    while index:
        index, r = divmod(index - 1, 26)
        s = chr(65 + r) + s
    return s


def _sheet_rows(files: LocalFileStorage, job_id: str) -> tuple[list[str], dict[int, list]]:
    if job_id not in _SHEET_CACHE:
        from openpyxl import load_workbook
        wb = load_workbook(files.get_input_path(job_id), read_only=True, data_only=True)
        ws = wb[INPUT_SHEET]
        rows = ws.iter_rows(values_only=True)
        header = [("" if h is None else str(h)) for h in next(rows)]
        while header and header[-1] == "":
            header.pop()
        cells = {i + 2: list(r[: len(header)]) for i, r in enumerate(rows)}
        wb.close()
        _SHEET_CACHE[job_id] = (header, cells)
    return _SHEET_CACHE[job_id]


@router.get("/{job_id}/web-evidence")
def web_evidence(job_id: str, repos: Annotated[MongoRepositories, Depends(repositories)],
                 files: Annotated[LocalFileStorage, Depends(storage)]) -> dict:
    _job(repos, job_id)
    docs = repos.web_evidence(job_id)
    header, cells = _sheet_rows(files, job_id)
    rows = []
    for d in docs:
        raw = cells.get(int(d["row_number"])) or []
        rows.append({"cells": [("" if v is None else v) for v in raw] + [""] * (len(header) - len(raw)), **d, "evaluation": evaluate(d)})
    run = repos.web_evidence_run(job_id)
    columns = [{"letter": _column_letter(i + 1), "label": h} for i, h in enumerate(header)]
    return {"status": (run or {}).get("status", "NOT_RUN"), "run": run, "rows": rows, "columns": columns,
            "next_letter_index": len(header) + 1, "sheet": INPUT_SHEET}
