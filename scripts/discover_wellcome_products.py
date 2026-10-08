"""Discover exact Wellcome product pages for every live item in a processed job.

This is deliberately a web-discovery task only. It stores page metadata and image URLs;
it never downloads product images and never invokes an AI model.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock

from app.config import get_settings
from app.repositories.mongo import MongoRepositories
from app.services.web_check_service import ERROR, FOUND, NOT_FOUND, NOT_MATCHED, WellcomeClient


TERMINAL_STATUSES = {FOUND, NOT_FOUND, NOT_MATCHED, ERROR}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id", help="Processed job whose non-purged items will be checked")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--retry-not-found", action="store_true")
    parser.add_argument("--retry-errors", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()
    repositories = MongoRepositories(settings.mongodb_uri, settings.mongodb_db)
    query = {"job_id": args.job_id, "route": {"$ne": "SKIPPED_PURGED"}}
    items = list(repositories.db.job_items.find(query, {"_id": 0, "item_no": 1, "route": 1}))
    item_routes = {
        str(item.get("item_no") or "").strip(): item.get("route")
        for item in items
        if str(item.get("item_no") or "").strip()
    }
    known = repositories.web_lookups(list(item_routes))

    def should_run(item_no: str) -> bool:
        status = (known.get(item_no) or {}).get("status")
        if status is None:
            return True
        if status == NOT_FOUND:
            return args.retry_not_found
        if status in {ERROR, NOT_MATCHED}:
            return args.retry_errors
        return False

    pending = [item_no for item_no in item_routes if should_run(item_no)]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    repositories.db.wellcome_discovery_runs.insert_one({
        "run_id": run_id,
        "job_id": args.job_id,
        "status": "RUNNING",
        "live_items": len(item_routes),
        "pending_items": len(pending),
        "workers": args.workers,
        "started_at": datetime.now(timezone.utc),
        "image_analysis": False,
    })
    print(f"run={run_id} live={len(item_routes)} cached={len(known)} pending={len(pending)}", flush=True)

    counts: Counter[str] = Counter()
    lock = Lock()
    # One client per worker prevents shared timing state from racing. No store is passed:
    # this runner owns persistence so every terminal outcome is resumable.
    clients = [WellcomeClient(delay=1.0, timeout=20, store=None) for _ in range(args.workers)]

    def lookup(index_item: tuple[int, str]) -> tuple[str, dict]:
        index, item_no = index_item
        result = clients[index % len(clients)].lookup(item_no)
        result = {k: value for k, value in result.items() if k != "reused"}
        result["route"] = item_routes[item_no]
        result["discovery_run_id"] = run_id
        repositories.save_web_lookup(item_no, result)
        return item_no, result

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for completed, (item_no, result) in enumerate(pool.map(lookup, enumerate(pending)), start=1):
                status = result.get("status") or ERROR
                with lock:
                    counts[status] += 1
                if completed == 1 or completed % 100 == 0 or completed == len(pending):
                    repositories.db.wellcome_discovery_runs.update_one(
                        {"run_id": run_id},
                        {"$set": {"completed_items": completed, "counts": dict(counts), "updated_at": datetime.now(timezone.utc)}},
                    )
                    print(f"progress={completed}/{len(pending)} counts={dict(counts)} last={item_no}", flush=True)
    except BaseException as exc:
        repositories.db.wellcome_discovery_runs.update_one(
            {"run_id": run_id},
            {"$set": {"status": "FAILED", "counts": dict(counts), "error": f"{type(exc).__name__}: {exc}", "finished_at": datetime.now(timezone.utc)}},
        )
        raise

    repositories.db.wellcome_discovery_runs.update_one(
        {"run_id": run_id},
        {"$set": {"status": "READY", "completed_items": len(pending), "counts": dict(counts), "finished_at": datetime.now(timezone.utc)}},
    )
    print(f"complete counts={dict(counts)}", flush=True)


if __name__ == "__main__":
    main()
