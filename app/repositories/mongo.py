import re
from datetime import datetime, timezone
from typing import Any, Iterable

from pymongo import ASCENDING, DESCENDING, MongoClient, ReplaceOne
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
        self.db.open_question_answers.create_index("category", unique=True)
        self.db.job_accuracy.create_index("job_id", unique=True)
        self.db.lane_a_trials.create_index([("job_id", ASCENDING), ("saved_at", DESCENDING)])
        self.db.lane_a_trial_rows.create_index([("job_id", ASCENDING), ("run_id", ASCENDING), ("row_number", ASCENDING)], unique=True)
        self.db.job_comparisons.create_index(
            [("past_job_id", ASCENDING), ("new_job_id", ASCENDING)], unique=True,
        )
        self.db.job_comparison_rows.create_index(
            [("past_job_id", ASCENDING), ("new_job_id", ASCENDING), ("row_number", ASCENDING)],
            unique=True,
        )
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

    # Atlas shared clusters (M0) block every write past 512 MB of data. The limit is
    # configurable so a larger cluster can raise it or switch the check off with 0.
    STORAGE_LIMIT_MB = 512

    def storage_headroom(self) -> tuple[float, float] | None:
        """(used MB, limit MB) of the results database, or None when not enforced."""
        if not self.STORAGE_LIMIT_MB:
            return None
        try:
            stats = self.db.command("dbstats")
        except Exception:  # noqa: BLE001 - never let a stats failure block processing
            return None
        return float(stats.get("dataSize", 0)) / 1e6, float(self.STORAGE_LIMIT_MB)

    def delete_items(self, job_id: str) -> int:
        return self.db.job_items.delete_many({"job_id": job_id}).deleted_count

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

    def comparison_items(self, job_id: str) -> list[dict[str, Any]]:
        """The projection the past-vs-new comparison needs: statuses, values, findings
        and review decisions, without raw agent payloads (a 13k-row job is fetched twice)."""
        projection = {
            "_id": 0,
            "row_number": 1,
            "item_no": 1,
            "group": 1,
            "method": 1,
            "reason_code": 1,
            "application_policy": 1,
            "context.item_desc_eng": 1,
            "context.item_desc_local_lang": 1,
            "context.web_description_eng": 1,
            "context.web_description_chi": 1,
            "original": 1,
            "field_proposals": 1,
            "changes": 1,
            "findings": 1,
            "review.overall_status": 1,
            "review.override_values": 1,
        }
        return list(
            self.db.job_items.find({"job_id": job_id}, projection)
            .sort("row_number", ASCENDING)
            .batch_size(1000)
        )

    def attention_items(self, job_id: str) -> list[dict[str, Any]]:
        """Every row that is not simply already correct or purged: the rows the
        open-questions view groups. A few hundred rows, so read in one go."""
        projection = {
            "_id": 0, "row_number": 1, "item_no": 1, "group": 1, "method": 1,
            "reason_code": 1, "application_policy": 1, "context": 1, "original": 1,
            "field_proposals": 1, "changes": 1, "findings": 1,
            "review.overall_status": 1, "review.override_values": 1,
        }
        query = {
            "job_id": job_id,
            "$nor": [status_query("NO_CHANGE"), status_query("SKIPPED")],
        }
        return list(self.db.job_items.find(query, projection).sort("row_number", ASCENDING))

    def accuracy_items(self, job_id: str) -> list[dict[str, Any]]:
        """Every live row, narrow: what the accuracy sets need and nothing more."""
        projection = {
            "_id": 0, "row_number": 1, "item_no": 1, "group": 1, "reason_code": 1,
            "application_policy": 1, "original": 1, "field_proposals": 1, "findings.code": 1,
            "context.category": 1, "context.item_desc_eng": 1, "context.item_desc_local_lang": 1,
            "context.web_description_eng": 1, "context.web_description_chi": 1,
        }
        return list(self.db.job_items.find({"job_id": job_id, "group": {"$ne": "SKIPPED_PURGED"}}, projection)
                    .sort("row_number", ASCENDING).batch_size(2000))

    def latest_reading_test(self, file_name: str | None) -> dict[str, Any] | None:
        """The most recent completed blind reading test on any run of the same workbook:
        the one place where the reader is measured against known answers."""
        query: dict[str, Any] = {"ai_reading_test.status": "COMPLETED"}
        if file_name:
            query["original_file_name"] = file_name
        job = self.db.jobs.find_one(query, {"_id": 0, "job_id": 1, "ai_reading_test": 1, "created_at": 1},
                                    sort=[("ai_reading_test.finished_at", DESCENDING)])
        if not job:
            return None
        test = job["ai_reading_test"]
        return {"job_id": job["job_id"], "finished_at": test.get("finished_at"), "prompt_version": test.get("prompt_version"),
                "score": test.get("score")}

    def job_accuracy(self, job_id: str) -> dict[str, Any] | None:
        return public(self.db.job_accuracy.find_one({"job_id": job_id}))

    def save_job_accuracy(self, document: dict[str, Any]) -> None:
        self.db.job_accuracy.replace_one({"job_id": document["job_id"]}, {**document, "saved_at": now()}, upsert=True)

    def items_by_rows(self, job_id: str, row_numbers: list[int]) -> list[dict[str, Any]]:
        projection = {"_id": 0, "row_number": 1, "item_no": 1, "group": 1, "original": 1, "field_proposals": 1,
                      "application_policy": 1, "reason_code": 1, "findings": 1, "changes": 1, "method": 1,
                      "context": 1, "review.overall_status": 1, "review.override_values": 1}
        return list(self.db.job_items.find({"job_id": job_id, "row_number": {"$in": row_numbers}}, projection).sort("row_number", ASCENDING))

    def open_question_answers(self) -> dict[str, dict[str, Any]]:
        return {
            doc["category"]: public(doc)
            for doc in self.db.open_question_answers.find({}, {"_id": 0})
        }

    def save_open_question_answer(self, document: dict[str, Any]) -> None:
        self.db.open_question_answers.replace_one(
            {"category": document["category"]}, document, upsert=True,
        )

    # --- Lane A reasoning trial (shadow only) ----------------------------------
    def save_lane_a_trial(self, document: dict[str, Any], rows: Iterable[dict[str, Any]]) -> None:
        key = {"job_id": document["job_id"], "run_id": document["run_id"]}
        self.db.lane_a_trials.replace_one(key, {**document, "saved_at": now()}, upsert=True)
        self.db.lane_a_trial_rows.delete_many(key)
        batch = [{**key, **row} for row in rows]
        if batch:
            self.db.lane_a_trial_rows.insert_many(batch)

    def latest_lane_a_trial(self, job_id: str) -> dict[str, Any] | None:
        return public(self.db.lane_a_trials.find_one({"job_id": job_id}, sort=[("saved_at", DESCENDING)]))

    def lane_a_trial_rows(self, job_id: str, run_id: str) -> list[dict[str, Any]]:
        return list(self.db.lane_a_trial_rows.find({"job_id": job_id, "run_id": run_id}, {"_id": 0}).sort("row_number", ASCENDING))

    def lane_a_trial_row(self, job_id: str, row_number: int) -> dict[str, Any] | None:
        latest = self.latest_lane_a_trial(job_id)
        if not latest:
            return None
        return public(self.db.lane_a_trial_rows.find_one({"job_id": job_id, "run_id": latest["run_id"], "row_number": row_number}, {"_id": 0}))

    def previous_completed_job(self, job: dict[str, Any]) -> dict[str, Any] | None:
        """The most recent earlier finished run of the same workbook, if any."""
        return self.db.jobs.find_one(
            {
                "job_id": {"$ne": job.get("job_id")},
                "original_file_name": job.get("original_file_name"),
                "created_at": {"$lt": job.get("created_at")},
                "status": {"$in": [
                    "READY_FOR_REVIEW", "REVIEW_IN_PROGRESS", "READY_TO_EXPORT", "EXPORTING", "EXPORTED",
                ]},
            },
            {"_id": 0},
            sort=[("created_at", DESCENDING)],
        )

    # --- past run versus new run -------------------------------------------------
    # The comparison of two 13k-row jobs takes minutes to read from Atlas, so it is
    # built once in the background and stored: one summary document plus one document
    # per compared row, which pages and filters like the ledger.

    def job_comparison(self, past_job_id: str, new_job_id: str) -> dict[str, Any] | None:
        return public(self.db.job_comparisons.find_one(
            {"past_job_id": past_job_id, "new_job_id": new_job_id},
        ))

    def save_job_comparison(self, document: dict[str, Any]) -> None:
        key = {"past_job_id": document["past_job_id"], "new_job_id": document["new_job_id"]}
        self.db.job_comparisons.replace_one(key, {**document, "updated_at": now()}, upsert=True)

    def replace_job_comparison_rows(
        self, past_job_id: str, new_job_id: str, rows: Iterable[dict[str, Any]],
    ) -> None:
        key = {"past_job_id": past_job_id, "new_job_id": new_job_id}
        self.db.job_comparison_rows.delete_many(key)
        batch: list[dict[str, Any]] = []
        for row in rows:
            batch.append({**key, **row})
            if len(batch) >= 1000:
                self.db.job_comparison_rows.insert_many(batch)
                batch = []
        if batch:
            self.db.job_comparison_rows.insert_many(batch)

    def job_comparison_rows(
        self, past_job_id: str, new_job_id: str, filters: dict[str, Any], page: int, page_size: int,
    ) -> tuple[list[dict[str, Any]], int]:
        query: dict[str, Any] = {"past_job_id": past_job_id, "new_job_id": new_job_id}
        if filters.get("changed_only"):
            query["change"] = {"$ne": "SAME"}
        if filters.get("change"):
            query["change"] = filters["change"]
        if filters.get("past_status"):
            query["past.status"] = filters["past_status"]
        if filters.get("new_status"):
            query["new.status"] = filters["new_status"]
        if filters.get("group"):
            query["group"] = filters["group"]
        search = str(filters.get("search") or "").strip()
        if search:
            pattern = {"$regex": re.escape(search), "$options": "i"}
            query["$or"] = [{"item_no": pattern}, {"product": pattern}, {"product_local": pattern}]
        total = self.db.job_comparison_rows.count_documents(query)
        rows = list(
            self.db.job_comparison_rows.find(query, {"_id": 0, "past_job_id": 0, "new_job_id": 0})
            .sort("row_number", ASCENDING)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        return rows, total

    def export_items(self, job_id: str) -> list[dict[str, Any]]:
        projection = {
            "_id": 0,
            "row_number": 1,
            "item_no": 1,
            "group": 1,
            "original": 1,
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
            "verification": 1, "guards": 1, "department": 1,
        }
        return list(self.db.job_items.find({"job_id": job_id}, projection).sort("row_number", ASCENDING))

    def ai_reading_results(self, job_id: str) -> list[dict[str, Any]]:
        return list(self.db.ai_reading_results.find({"job_id": job_id}, {"_id": 0}))

    def replace_ai_reading_results(self, job_id: str, results: list[dict[str, Any]]) -> None:
        """Per-product audit trail of the latest AI reading test for a job."""
        self.db.ai_reading_results.delete_many({"job_id": job_id})
        if results:
            self.db.ai_reading_results.insert_many([dict(result) for result in results])

    def save_agent_evaluation(self, document: dict[str, Any]) -> None:
        self.db.agent_evaluations.insert_one({**document, "created_at": now()})

    def agent_evaluations(self, limit: int = 10) -> list[dict[str, Any]]:
        """Newest first, so versions of the agent can be compared."""
        cursor = self.db.agent_evaluations.find({}, {"_id": 0}).sort("created_at", -1).limit(limit)
        return list(cursor)

    def close(self) -> None:
        self.client.close()
