from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

from app.agents.provider import (
    InferenceProvider,
    InferenceRequest,
    InvalidInferenceResponseError,
    validate_evidence,
)
from app.domain.enums import ProposalMethod, WorkGroup
from app.domain.product import InputProduct
from app.repositories.mongo import MongoRepositories
from app.rules.registry import RuleRegistry
from app.services.classifier import classify
from app.services.discrepancy_service import find_discrepancies
from app.services.excel_reader import ExcelReader, WorkbookRow
from app.services.purge_detector import is_purged
from app.services.rule_engine import RuleEngine
from app.storage.local import LocalFileStorage


EXPECTED_V02 = {
    "workbook_rows": 66082,
    "department_rows": 13300,
    "purged": 758,
    "live": 12542,
    "group_a": 11975,
    "group_b": 512,
    "group_c": 55,
    "data_shape_error": 0,
}


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _original(product: InputProduct) -> dict[str, object]:
    return {
        "standard_size": _json_value(product.standard_size),
        "standard_uom": product.standard_uom,
        "standard_pack_size": _json_value(product.standard_pack_size),
        "legacy_size": _json_value(product.legacy_size),
        "legacy_uom": product.legacy_uom,
    }


def _context(product: InputProduct) -> dict[str, object]:
    return {
        "division": product.division,
        "category": product.category,
        "subcategory": product.subcategory,
        "section": product.section,
        "item_brand_eng": product.item_brand_eng,
        "item_brand_local_lang": product.item_brand_local,
        "item_desc_eng": product.item_desc_eng,
        "item_desc_local_lang": product.item_desc_local,
        "web_description_eng": product.web_description_eng,
        "web_description_chi": product.web_description_chi,
    }


class JobProcessor:
    def __init__(
        self,
        repositories: MongoRepositories,
        storage: LocalFileStorage,
        registry: RuleRegistry,
        inference_provider: InferenceProvider,
        default_rounding_decimals: int | None = None,
        ai_max_concurrency: int = 5,
    ):
        self.repositories = repositories
        self.storage = storage
        self.registry = registry
        self.inference_provider = inference_provider
        self.reader = ExcelReader()
        self.rule_engine = RuleEngine(registry, default_rounding_decimals)
        self.ai_max_concurrency = ai_max_concurrency

    def process(self, job_id: str) -> None:
        try:
            self._process(job_id)
        except Exception as exc:
            self.repositories.update_job(job_id, {
                "status": "FAILED", "progress.stage": "FAILED", "error": str(exc)
            })
            raise

    def _process(self, job_id: str) -> None:
        job = self.repositories.get_job(job_id)
        if not job:
            raise ValueError("job not found")
        selected = set(job["selected_departments"])
        self.repositories.update_job(job_id, {
            "status": "PROCESSING", "progress.stage": "PROFILING", "error": None
        })

        counts: Counter[str] = Counter()
        source_counts: Counter[str] = Counter()
        items: list[dict[str, Any]] = []
        group_c: list[tuple[int, InputProduct]] = []
        for workbook_row in self.reader.iter_rows(self.storage.get_input_path(job_id)):
            counts["workbook_rows"] += 1
            product = workbook_row.product
            if product.department not in selected:
                continue
            counts["department_rows"] += 1
            purged = is_purged(workbook_row.raw)
            group = classify(product, purged=purged)
            if purged:
                counts["purged"] += 1
            else:
                counts["live"] += 1
            counts[f"group_{group.value.lower()}"] += 1
            item = self._base_item(job_id, workbook_row, group)

            if group == WorkGroup.B:
                subtype = "B2" if product.standard_size is None and product.standard_uom is None else "B1"
                counts[f"group_{subtype.lower()}"] += 1
                source_uom = product.legacy_uom
                if source_uom:
                    source_counts[source_uom] += 1
                proposal = self.rule_engine.propose(product.legacy_size, source_uom)
                if proposal:
                    item["field_proposals"].update({
                        "standard_size": str(proposal.standard_size),
                        "standard_uom": proposal.standard_uom,
                    })
                    item["method"] = ProposalMethod.RULE.value
                    item["reason_code"] = "RULE_CONVERSION"
                    item["rule"] = proposal.model_dump(mode="json")
                else:
                    item["reason_code"] = "NO_RULE" if self.registry.get(source_uom) is None else "MALFORMED_VALUE"
                item["review"] = self._pending_review(size_uom=True)
            elif group == WorkGroup.C:
                group_c.append((len(items), product))
                item["review"] = self._pending_review(size_uom=True)
            items.append(item)

        self.repositories.update_job(job_id, {"progress.stage": "PROCESSING_DESCRIPTIONS"})
        if group_c:
            asyncio.run(self._infer_group_c(items, group_c))

        self.repositories.update_job(job_id, {"progress.stage": "CHECKING_DISCREPANCIES"})
        discrepancies = 0
        for item in items:
            if item["discrepancy"]["flagged"]:
                discrepancies += 1
        counts["discrepancies"] = discrepancies
        stats = {
            "workbook_rows": counts["workbook_rows"],
            "department_rows": counts["department_rows"],
            "purged": counts["purged"],
            "live": counts["live"],
            "group_a": counts["group_a"],
            "group_b": counts["group_b"],
            "group_b1": counts["group_b1"],
            "group_b2": counts["group_b2"],
            "group_c": counts["group_c"],
            "data_shape_error": counts["group_data_shape_error"],
            "discrepancies": discrepancies,
        }
        if job.get("snapshot_label") == "v0.2":
            differences = {key: (stats[key], expected) for key, expected in EXPECTED_V02.items() if stats[key] != expected}
            if differences:
                raise ValueError(f"v0.2 profile invariant failed: {differences}")

        for offset in range(0, len(items), 1000):
            self.repositories.replace_items(items[offset:offset + 1000])
        covered = sorted(source for source in source_counts if self.registry.get(source))
        uncovered = sorted(source for source in source_counts if not self.registry.get(source))
        readiness = {
            "ruleset_version": self.registry.version,
            "ruleset_checksum": self.registry.checksum,
            "covered_source_uoms": covered,
            "uncovered_source_uoms": uncovered,
            "uncovered_affected_rows": sum(source_counts[source] for source in uncovered),
        }
        self.repositories.update_job(job_id, {
            "status": "READY_FOR_REVIEW",
            "stats": stats,
            "rule_readiness": readiness,
            "progress": {
                "stage": "READY_FOR_REVIEW", "processed": stats["live"],
                "total": stats["live"], "percent": 100,
            },
        })

    def _base_item(self, job_id: str, row: WorkbookRow, group: WorkGroup) -> dict[str, Any]:
        details = [] if group == WorkGroup.SKIPPED_PURGED else find_discrepancies(row.product)
        return {
            "job_id": job_id,
            "row_number": row.row_number,
            "item_no": row.product.item_no,
            "department": row.product.department,
            "group": group.value,
            "context": _context(row.product),
            "original": _original(row.product),
            "field_proposals": {"standard_size": None, "standard_uom": None, "standard_pack_size": None},
            "method": ProposalMethod.NONE.value,
            "reason_code": "ALREADY_BASE_UNIT" if group == WorkGroup.A else None,
            "rule": {"rule_id": None, "factor": None, "source_uom": None, "target_uom": None},
            "evidence": [],
            "confidence": None,
            "discrepancy": {"flagged": bool(details), "details": details},
            "review": self._not_required_review(),
        }

    @staticmethod
    def _not_required_review() -> dict[str, Any]:
        return {
            "field_decisions": {"standard_size": "NOT_REQUIRED", "standard_uom": "NOT_REQUIRED", "standard_pack_size": "NOT_REQUIRED"},
            "overall_status": "NOT_REQUIRED", "override_values": None, "comment": None,
        }

    @staticmethod
    def _pending_review(size_uom: bool) -> dict[str, Any]:
        return {
            "field_decisions": {
                "standard_size": "PENDING" if size_uom else "NOT_REQUIRED",
                "standard_uom": "PENDING" if size_uom else "NOT_REQUIRED",
                "standard_pack_size": "NOT_REQUIRED",
            },
            "overall_status": "PENDING", "override_values": None, "comment": None,
        }

    async def _infer_group_c(self, items: list[dict[str, Any]], candidates: list[tuple[int, InputProduct]]) -> None:
        semaphore = asyncio.Semaphore(self.ai_max_concurrency)

        async def infer(index: int, product: InputProduct) -> None:
            request = InferenceRequest(
                item_brand_eng=product.item_brand_eng,
                item_brand_local_lang=product.item_brand_local,
                item_desc_eng=product.item_desc_eng,
                item_desc_local_lang=product.item_desc_local,
                web_description_eng=product.web_description_eng,
                web_description_chi=product.web_description_chi,
            )
            try:
                async with semaphore:
                    response = await self.inference_provider.infer(request)
                result = response.result
                validate_evidence(request, result)
                item = items[index]
                item["method"] = ProposalMethod.AI_INFERENCE.value
                item["reason_code"] = result.reason_code
                item["confidence"] = result.confidence
                item["ai_provenance"] = response.metadata.model_dump(mode="json")
                observations = []
                if result.measurement:
                    observations.append(result.measurement)
                observations.extend(result.conflicting_measurements)
                item["evidence"] = [
                    {"field": observed.field, "fragment": observed.fragment}
                    for observed in observations
                ]
                if result.pack_evidence:
                    item["evidence"].append(result.pack_evidence.model_dump())
                if result.status == "PROPOSAL" and result.measurement:
                    item["ai_observation"] = result.measurement.model_dump(mode="json")
                    proposal = self.rule_engine.propose(
                        result.measurement.value,
                        result.measurement.uom,
                    )
                    if proposal is None:
                        item["reason_code"] = (
                            "NO_RULE"
                            if self.registry.get(result.measurement.uom) is None
                            else "MALFORMED_VALUE"
                        )
                    else:
                        item["field_proposals"].update({
                            "standard_size": str(proposal.standard_size),
                            "standard_uom": proposal.standard_uom,
                        })
                        item["rule"] = proposal.model_dump(mode="json")
                    if result.pack_size is not None:
                        item["field_proposals"]["standard_pack_size"] = str(result.pack_size)
                        item["review"]["field_decisions"]["standard_pack_size"] = "PENDING"
            except InvalidInferenceResponseError as exc:
                items[index]["reason_code"] = "AI_INVALID_RESPONSE"
                items[index]["ai_error"] = str(exc)
            except ValueError as exc:
                items[index]["reason_code"] = "AI_INVALID_RESPONSE"
                items[index]["ai_error"] = str(exc)
            except Exception as exc:
                items[index]["reason_code"] = "AI_PROVIDER_ERROR"
                items[index]["ai_error"] = str(exc)

        await asyncio.gather(*(infer(index, product) for index, product in candidates))
