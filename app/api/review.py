from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.api.dependencies import repositories
from app.domain.enums import BASE_UNITS
from app.repositories.mongo import MongoRepositories, now
from app.services.result_ledger_service import changes_after_review

router = APIRouter(prefix="/jobs", tags=["review"])
REVIEW_FIELDS = frozenset({"standard_size", "standard_uom", "standard_pack_size"})


class BulkDecision(BaseModel):
    fields: list[str] = Field(default_factory=lambda: ["standard_size", "standard_uom"])


class ItemDecision(BaseModel):
    action: Literal["APPROVE", "REJECT", "OVERRIDE"]
    fields: list[str] = Field(default_factory=list)
    values: dict[str, object] | None = None
    comment: str | None = None

    @model_validator(mode="after")
    def validate_action(self) -> "ItemDecision":
        if self.action == "OVERRIDE" and not self.values:
            raise ValueError("override values are required")
        return self


def _refresh_job_status(repos: MongoRepositories, job_id: str) -> None:
    status = "READY_TO_EXPORT" if repos.pending_count(job_id) == 0 else "REVIEW_IN_PROGRESS"
    # A review decision changes the workload figures, so the cached report is dropped.
    repos.update_job(job_id, {"status": status, "quality": None})


@router.post("/{job_id}/conversion-groups/{rule_id}/approve")
def approve_group(
    job_id: str,
    rule_id: str,
    payload: BulkDecision,
    repos: Annotated[MongoRepositories, Depends(repositories)],
) -> dict[str, int]:
    if set(payload.fields) != {"standard_size", "standard_uom"}:
        raise HTTPException(400, "Only size and UOM may be bulk approved")
    approved, skipped = repos.approve_conversion_group(job_id, rule_id)
    _refresh_job_status(repos, job_id)
    return {"approved": approved, "skipped": skipped}


class Verification(BaseModel):
    # None withdraws a verdict.
    verdict: Literal["CORRECT", "WRONG"] | None
    comment: str | None = Field(default=None, max_length=500)


@router.patch("/{job_id}/items/{row_number}/verification")
def verify_item(
    job_id: str,
    row_number: int,
    payload: Verification,
    repos: Annotated[MongoRepositories, Depends(repositories)],
) -> dict[str, object]:
    """A reviewer's verdict on what the agent produced for this product.

    It never changes K/L/M or the export. It only feeds the accuracy figures."""
    verification = None if payload.verdict is None else {
        "verdict": payload.verdict, "comment": payload.comment, "verified_at": now(),
    }
    if not repos.update_item(job_id, row_number, {"verification": verification}):
        raise HTTPException(404, "Item not found")
    repos.update_job(job_id, {"quality": None})
    return {"row_number": row_number, "verdict": payload.verdict}


@router.patch("/{job_id}/items/{row_number}/decision")
def decide_item(
    job_id: str,
    row_number: int,
    payload: ItemDecision,
    repos: Annotated[MongoRepositories, Depends(repositories)],
) -> dict[str, str]:
    rows, _ = repos.list_items(job_id, {"row_number": row_number}, 0, 1)
    if not rows:
        raise HTTPException(404, "Item not found")
    item = rows[0]
    fields = set(payload.fields) if payload.fields else {
        field for field, value in item["field_proposals"].items() if value is not None
    }
    if payload.action in {"APPROVE", "REJECT"} and not fields:
        fields = {"standard_size", "standard_uom"}
    if not fields.issubset(REVIEW_FIELDS):
        raise HTTPException(400, "Unknown review field")

    updates: dict[str, object] = {"review.comment": payload.comment}
    if payload.action == "OVERRIDE":
        values = dict(payload.values or {})
        if not set(values).issubset(REVIEW_FIELDS):
            raise HTTPException(400, "Unknown override field")
        uom = values.get("standard_uom")
        if uom is not None:
            uom = str(uom).strip().upper()
            if uom not in BASE_UNITS:
                raise HTTPException(400, f"UOM must be one of {sorted(BASE_UNITS)}")
            values["standard_uom"] = uom
        for field in ("standard_size", "standard_pack_size"):
            if field in values and values[field] is not None:
                try:
                    if Decimal(str(values[field])) <= 0:
                        raise ValueError
                except (InvalidOperation, ValueError):
                    raise HTTPException(400, f"{field} must be a positive number")
                values[field] = str(values[field])
        updates.update({
            "review.override_values": values,
            "review.overall_status": "OVERRIDDEN",
            "method": "HUMAN_OVERRIDE",
            "changes": changes_after_review(item, "OVERRIDDEN", values),
        })
        for field in values:
            updates[f"review.field_decisions.{field}"] = "OVERRIDDEN"
    else:
        decision = "APPROVED" if payload.action == "APPROVE" else "REJECTED"
        updates["review.overall_status"] = decision
        updates["changes"] = changes_after_review(item, decision)
        for field in fields:
            updates[f"review.field_decisions.{field}"] = decision
    repos.update_item(job_id, row_number, updates)
    _refresh_job_status(repos, job_id)
    return {"job_id": job_id, "row_number": str(row_number), "status": str(updates["review.overall_status"])}
