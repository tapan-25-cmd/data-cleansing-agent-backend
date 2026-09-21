from datetime import datetime, timezone
from typing import Any, Iterable

from pymongo import ASCENDING, MongoClient, ReplaceOne
from pymongo.database import Database

from app.services.result_status import STATUSES, status_query


def now() -> datetime:
    return datetime.now(timezone.utc)


def public(document: dict[str, Any] | None) -> dict[str, Any] | None:
    if document is None:
        return None
    result = dict(document)
    result.pop("_id", None)
    return result


class MongoRepositories:
    def __init__(self, uri: str, database_name: str):
        self.client: MongoClient = MongoClient(uri, serverSelectionTimeoutMS=3000)
        self.db: Database = self.client[database_name]

    def ensure_indexes(self) -> None:
        self.db.jobs.create_index("job_id", unique=True)
        self.db.jobs.create_index([("created_at", ASCENDING)])
        self.db.jobs.create_index("status")
        self.db.job_items.create_index([("job_id", ASCENDING), ("row_number", ASCENDING)], unique=True)
        self.db.job_items.create_index([("job_id", ASCENDING), ("group", ASCENDING)])
        self.db.job_items.create_index([("job_id", ASCENDING), ("review.overall_status", ASCENDING)])
        self.db.job_items.create_index([("job_id", ASCENDING), ("rule.rule_id", ASCENDING)])
        self.db.job_items.create_index([("job_id", ASCENDING), ("application_policy", ASCENDING)])
        self.db.job_items.create_index([("job_id", ASCENDING), ("findings.category", ASCENDING)])
        self.db.job_items.create_index([("job_id", ASCENDING), ("findings.severity", ASCENDING)])
        self.db.ai_reading_results.create_index([("job_id", ASCENDING), ("row_number", ASCENDING)])

    def create_job(self, document: dict[str, Any]) -> None:
        timestamp = now()
        self.db.jobs.insert_one({**document, "created_at": timestamp, "updated_at": timestamp})

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return public(self.db.jobs.find_one({"job_id": job_id}))

    def update_job(self, job_id: str, values: dict[str, Any]) -> None:
        self.db.jobs.update_one({"job_id": job_id}, {"$set": {**values, "updated_at": now()}})

    def replace_items(self, documents: Iterable[dict[str, Any]]) -> None:
        operations = [
            ReplaceOne(
                {"job_id": item["job_id"], "row_number": item["row_number"]},
                item,
                upsert=True,
            )
            for item in documents
        ]
        if operations:
            self.db.job_items.bulk_write(operations, ordered=False)

    def list_items(self, job_id: str, query: dict[str, Any], skip: int, limit: int) -> tuple[list[dict[str, Any]], int]:
        selector = {"job_id": job_id, **query}
        total = self.db.job_items.count_documents(selector)
        cursor = self.db.job_items.find(selector, {"_id": 0}).sort("row_number", ASCENDING).skip(skip).limit(limit)
        return list(cursor), total

    def update_item(self, job_id: str, row_number: int, values: dict[str, Any]) -> bool:
        result = self.db.job_items.update_one(
            {"job_id": job_id, "row_number": row_number}, {"$set": values}
        )
        return result.matched_count == 1

    def get_item(self, job_id: str, row_number: int) -> dict[str, Any] | None:
        return public(self.db.job_items.find_one({
            "job_id": job_id,
            "row_number": row_number,
        }))

    def result_facets(self, job_id: str) -> dict[str, dict[str, int]]:
        fields = {
            "group": "$group",
            "review_status": "$review.overall_status",
            "finding_category": "$findings.category",
            "finding_severity": "$findings.severity",
        }
        result: dict[str, dict[str, int]] = {}
        for name, expression in fields.items():
            rows = self.db.job_items.aggregate([
                {"$match": {"job_id": job_id}},
                {"$unwind": "$findings"} if name.startswith("finding_") else {"$match": {}},
                {"$group": {"_id": expression, "count": {"$sum": 1}}},
                {"$match": {"_id": {"$ne": None}}},
                {"$sort": {"_id": 1}},
            ])
            result[name] = {str(row["_id"]): int(row["count"]) for row in rows}
        # Statuses are derived, so each is counted with the same filter the ledger uses.
        result["status"] = {
            status: self.db.job_items.count_documents({"job_id": job_id, **status_query(status)})
            for status in STATUSES
        }
        return result

    def conversion_groups(self, job_id: str) -> list[dict[str, Any]]:
        pipeline = [
            {"$match": {"job_id": job_id, "group": "B"}},
            {"$group": {
                "_id": "$rule.rule_id",
                "rows": {"$sum": 1},
                "pending": {"$sum": {"$cond": [{"$eq": ["$review.overall_status", "PENDING"]}, 1, 0]}},
                "approved": {"$sum": {"$cond": [{"$eq": ["$review.overall_status", "APPROVED"]}, 1, 0]}},
                "source_uom": {"$first": "$rule.source_uom"},
                "target_uom": {"$first": "$rule.target_uom"},
                "factor": {"$first": "$rule.factor"},
            }},
            {"$sort": {"_id": 1}},
        ]
        return [
            {"rule_id": row.pop("_id") or "NO_RULE", **row}
            for row in self.db.job_items.aggregate(pipeline)
        ]

    def approve_conversion_group(self, job_id: str, rule_id: str) -> tuple[int, int]:
        selector = {
            "job_id": job_id,
            "group": "B",
            "rule.rule_id": rule_id,
            "review.overall_status": "PENDING",
            "discrepancy.flagged": False,
            "field_proposals.standard_size": {"$ne": None},
            "field_proposals.standard_uom": {"$ne": None},
        }
        eligible = self.db.job_items.count_documents(selector)
        result = self.db.job_items.update_many(selector, {"$set": {
            "review.field_decisions.standard_size": "APPROVED",
            "review.field_decisions.standard_uom": "APPROVED",
            "review.overall_status": "APPROVED",
        }})
        group_total = self.db.job_items.count_documents({
            "job_id": job_id, "group": "B", "rule.rule_id": rule_id
        })
        return result.modified_count, group_total - eligible

    def pending_count(self, job_id: str) -> int:
        return self.db.job_items.count_documents({
            "job_id": job_id, "review.overall_status": "PENDING"
        })

    def all_items(self, job_id: str) -> list[dict[str, Any]]:
        return list(self.db.job_items.find({"job_id": job_id}, {"_id": 0}).sort("row_number", ASCENDING))

    def export_items(self, job_id: str) -> list[dict[str, Any]]:
        projection = {
            "_id": 0,
            "row_number": 1,
            "item_no": 1,
            "group": 1,
            "field_proposals": 1,
            "review.overall_status": 1,
            "review.override_values": 1,
            "review.comment": 1,
            "application_policy": 1,
            "findings": 1,
            "changes": 1,
            "method": 1,
            "reason_code": 1,
            "result_ledger_version": 1,
        }
        return list(self.db.job_items.find({"job_id": job_id}, projection).sort("row_number", ASCENDING))

    def quality_items(self, job_id: str) -> list[dict[str, Any]]:
        projection = {
            "_id": 0, "row_number": 1, "item_no": 1, "group": 1, "original": 1,
            "context": 1, "findings.code": 1, "findings.human_reason": 1,
            "reason_code": 1, "pack_result.status": 1, "application_policy": 1,
            "review.overall_status": 1, "field_proposals": 1, "rule.rule_id": 1,
            "verification": 1,
        }
        return list(self.db.job_items.find({"job_id": job_id}, projection).sort("row_number", ASCENDING))

    def ai_reading_results(self, job_id: str) -> list[dict[str, Any]]:
        return list(self.db.ai_reading_results.find({"job_id": job_id}, {"_id": 0}))

    def replace_ai_reading_results(self, job_id: str, results: list[dict[str, Any]]) -> None:
        """Per-product audit trail of the latest AI reading test for a job."""
        self.db.ai_reading_results.delete_many({"job_id": job_id})
        if results:
            self.db.ai_reading_results.insert_many([dict(result) for result in results])

    def close(self) -> None:
        self.client.close()
