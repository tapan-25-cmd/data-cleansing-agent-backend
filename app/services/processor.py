from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from decimal import Decimal
from time import monotonic, perf_counter
from typing import Any, Callable

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
from app.services import guards
from app.services.category_profile import CategoryProfile, levels_of
from app.services.classifier import classify
from app.services.discrepancy_service import (
    DISCREPANCY_ENGINE_VERSION,
    DiscrepancyReport,
    analyze_discrepancies,
)
from app.services.excel_reader import FIELD_MAP, ExcelReader, WorkbookRow
from app.services.group_a_validator import (
    GROUP_A_VALIDATION_VERSION,
    STANDARD_UOM_ALIAS_CHECKSUM,
    STANDARD_UOM_ALIAS_VERSION,
    GroupAValidationResult,
    GroupAValidationStatus,
    GroupAValidator,
)
from app.services.pack_size_service import (
    PACK_EXTRACTION_VERSION,
    PackAssessment,
    PackSizeService,
    PackStatus,
)
from app.services.packaging_expression_service import PACKAGING_EXPRESSION_VERSION
from app.services.purge_detector import is_purged
from app.services.quality_service import QualityService
from app.services.rule_engine import RuleEngine
from app.services.result_ledger_service import enrich_result_item
from app.services.result_status import GROUPS, outcome_group
from app.services.gap_fill_service import GapFillService
from app.storage.local import LocalFileStorage

logger = logging.getLogger(__name__)


EXPECTED_V02 = {
    # The route counts: which method the tool used on each row. They check the file's
    # shape against the PRD baseline. The outcome groups (A/B/C) are reported beside
    # them and are not part of this check, because they depend on what the tool finds.
    "workbook_rows": 66082,
    "department_rows": 13300,
    "purged": 758,
    "live": 12542,
    # Five formerly trusted A rows now route to review because their explicit
    # case hierarchy conflicts with existing K/L/M (2 KIKI + 3 yoghurt rows).
    "route_a": 11970,
    "route_b": 512,
    "route_b1": 304,
    "route_b2": 170,
    "route_b3": 38,
    "route_c": 55,
    "validation_review": 5,
    "route_incomplete": 0,
    "data_shape_error": 0,
}


def _log_event(event: str, **fields: object) -> None:
    logger.info(json.dumps({"event": event, **fields}, default=str, separators=(",", ":")))


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _decimal_or_none(value: object) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except (ArithmeticError, ValueError):
        return None


def _original(product: InputProduct) -> dict[str, object]:
    return {
        "standard_size": _json_value(
            product.raw_standard_size
            if product.raw_standard_size is not None
            else product.standard_size
        ),
        "standard_uom": _json_value(
            product.raw_standard_uom
            if product.raw_standard_uom is not None
            else product.standard_uom
        ),
        "standard_pack_size": _json_value(
            product.raw_standard_pack_size
            if product.raw_standard_pack_size is not None
            else product.standard_pack_size
        ),
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


# Share of the overall bar given to each stage, in the order they run.
_STAGE_SPAN = {
    "PROFILING": (0, 35),
    "PROCESSING_RULES": (35, 55),
    "PROCESSING_DESCRIPTIONS": (55, 85),
    "CHECKING_DISCREPANCIES": (85, 88),
    "SAVING_RESULTS": (88, 99),
}


class ProgressReporter:
    """Persist live progress without writing to MongoDB on every row."""

    def __init__(
        self,
        repositories: MongoRepositories,
        job_id: str,
        min_interval_seconds: float = 0.75,
    ):
        self.repositories = repositories
        self.job_id = job_id
        self.min_interval_seconds = min_interval_seconds
        self._stage = "PROFILING"
        self._total = 0
        self._last_write = 0.0

    def start(self, stage: str, total: int = 0) -> None:
        self._stage = stage
        self._total = total
        self._write(0)

    def set_total(self, total: int) -> None:
        self._total = total

    def advance(self, processed: int) -> None:
        """Throttled; the final row of a stage is always written."""
        if processed >= self._total > 0 or (
            monotonic() - self._last_write >= self.min_interval_seconds
        ):
            self._write(processed)

    def _write(self, processed: int) -> None:
        low, high = _STAGE_SPAN.get(self._stage, (0, 0))
        fraction = min(processed / self._total, 1) if self._total else 0
        self._last_write = monotonic()
        self.repositories.update_job(self.job_id, {"progress": {
            "stage": self._stage,
            "processed": processed,
            # Zero means the stage size is not known yet (workbook still streaming).
            "total": self._total,
            "unit": "AGENT_CALLS" if self._stage == "PROCESSING_DESCRIPTIONS" else "ROWS",
            "percent": round(low + (high - low) * fraction),
        }})


class JobProcessor:
    def __init__(
        self,
        repositories: MongoRepositories,
        storage: LocalFileStorage,
        registry: RuleRegistry,
        inference_provider: InferenceProvider,
        default_rounding_decimals: int | None = None,
        ai_max_concurrency: int = 5,
        pack_size_inference_enabled: bool = True,
    ):
        self.repositories = repositories
        self.storage = storage
        self.registry = registry
        self.inference_provider = inference_provider
        self.reader = ExcelReader()
        # Actual Group B unit conversions follow the agreed Excel-style
        # nearest-whole policy. B3 cleanup preserves existing numeric values.
        # Group C retains its independent policy through the regular engine.
        self.group_b_rule_engine = RuleEngine(registry, 0)
        self.rule_engine = RuleEngine(registry, default_rounding_decimals)
        self.group_a_validator = GroupAValidator(self.group_b_rule_engine)
        self.pack_size_service = PackSizeService()
        self.gap_filler = GapFillService(self.group_b_rule_engine, registry)
        self.quality_service = QualityService(registry)
        self.ai_max_concurrency = ai_max_concurrency
        self.pack_size_inference_enabled = pack_size_inference_enabled

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
        started = perf_counter()
        _log_event("job.processing_started", job_id=job_id, selected_departments=sorted(selected))
        self.repositories.update_job(job_id, {"status": "PROCESSING", "error": None})
        progress = ProgressReporter(self.repositories, job_id)
        progress.start("PROFILING")

        counts: Counter[str] = Counter()
        self.ai_usage: Counter[str] = Counter()
        source_counts: Counter[str] = Counter()
        items: list[dict[str, Any]] = []
        group_c: list[tuple[int, InputProduct]] = []
        group_b_pack_candidates: list[tuple[int, InputProduct, Decimal | str | None, str | None]] = []
        selected_rows: list[WorkbookRow] = []
        for workbook_row in self.reader.iter_rows(
            self.storage.get_input_path(job_id), on_total=progress.set_total,
        ):
            counts["workbook_rows"] += 1
            progress.advance(counts["workbook_rows"])
            product = workbook_row.product
            if product.department not in selected:
                continue
            counts["department_rows"] += 1
            selected_rows.append(workbook_row)

        item_number_counts = Counter(
            row.product.item_no for row in selected_rows if row.product.item_no
        )
        # Pass 1: validate every row. The validated rows then describe what is normal
        # for each category in *this* workbook, which the guards need before any
        # value is proposed.
        assessed: list[tuple[WorkbookRow, bool, DiscrepancyReport | None, GroupAValidationResult | None]] = []
        for workbook_row in selected_rows:
            product = workbook_row.product
            purged = is_purged(workbook_row.raw)
            row_discrepancies: DiscrepancyReport | None = None
            row_validation: GroupAValidationResult | None = None
            if not purged:
                # One multi-signal extraction per row feeds Group A validation, the
                # guards and the persisted discrepancy record.
                row_discrepancies = analyze_discrepancies(product)
                row_validation = self.group_a_validator.validate(
                    workbook_row,
                    duplicate_item_number=(
                        bool(product.item_no)
                        and item_number_counts[product.item_no] > 1
                    ),
                    discrepancies=row_discrepancies,
                )
            assessed.append((workbook_row, purged, row_discrepancies, row_validation))
        profile = CategoryProfile.from_validated(
            (levels_of(row.product), result.standard_uom, result.standard_size)
            for row, _, _, result in assessed
            if result is not None and result.status == GroupAValidationStatus.VALID
        )

        progress.start("PROCESSING_RULES", len(selected_rows))
        for row_index, (workbook_row, purged, discrepancies, validation) in enumerate(assessed, start=1):
            progress.advance(row_index)
            product = workbook_row.product
            group = classify(product, purged=purged)
            if not purged:
                if validation is not None:
                    if validation.status == GroupAValidationStatus.VALID:
                        group = WorkGroup.A
                    elif validation.status == GroupAValidationStatus.AUTO_FIX:
                        group = WorkGroup.B
                    elif validation.status == GroupAValidationStatus.REVIEW:
                        group = WorkGroup.VALIDATION_REVIEW
                    else:
                        group = WorkGroup.DATA_SHAPE_ERROR
            if purged:
                counts["purged"] += 1
            else:
                counts["live"] += 1
            counts[f"route_{group.value.lower()}"] += 1
            if validation and validation.has_warnings and group == WorkGroup.A:
                counts["route_a_validation_warnings"] += 1
            item = self._base_item(job_id, workbook_row, group, validation, discrepancies)

            if group == WorkGroup.B:
                if validation and validation.status == GroupAValidationStatus.AUTO_FIX:
                    subtype = "B3"
                else:
                    subtype = "B2" if product.standard_size is None and product.standard_uom is None else "B1"
                counts[f"route_{subtype.lower()}"] += 1
                if subtype == "B3":
                    normalized = validation.normalization_proposal()
                    size_requires_fix = any(
                        issue.field == "standard_size"
                        and issue.code == "NUMERIC_TEXT_NORMALIZATION"
                        for issue in validation.issues
                    )
                    uom_requires_fix = any(
                        issue.field == "standard_uom"
                        and issue.code == "UOM_CANONICALIZATION"
                        for issue in validation.issues
                    )
                    pack_requires_fix = any(
                        issue.field == "standard_pack_size"
                        and issue.code == "NUMERIC_TEXT_NORMALIZATION"
                        for issue in validation.issues
                    )
                    if size_requires_fix:
                        item["field_proposals"]["standard_size"] = normalized["standard_size"]
                        item["field_provenance"]["standard_size"] = self._rule_provenance(
                            "STANDARD_FIELDS_CANONICALIZATION"
                        )
                    if uom_requires_fix:
                        item["field_proposals"]["standard_uom"] = normalized["standard_uom"]
                        item["field_provenance"]["standard_uom"] = self._rule_provenance(
                            "STANDARD_FIELDS_CANONICALIZATION"
                        )
                    if pack_requires_fix:
                        item["field_proposals"]["standard_pack_size"] = normalized["standard_pack_size"]
                    item["method"] = ProposalMethod.RULE.value
                    item["reason_code"] = "STANDARD_FIELDS_NORMALIZATION"
                    item["rule"] = {
                        "rule_id": "STANDARD_FIELDS_CANONICALIZATION",
                        "factor": "1",
                        "source_uom": _json_value(product.raw_standard_uom),
                        "target_uom": validation.standard_uom,
                        "source_value": str(validation.standard_size),
                        "raw_target": str(validation.standard_size),
                        "final_target": normalized["standard_size"],
                        "rounding_decimals": None,
                    }
                    item["review"] = self._pending_review(
                        size_uom=size_requires_fix or uom_requires_fix,
                        pack=pack_requires_fix,
                    )
                else:
                    source_uom = product.legacy_uom
                    if source_uom:
                        source_counts[source_uom] += 1
                    proposal = self.group_b_rule_engine.propose(product.legacy_size, source_uom)
                    view = profile.view(levels_of(product))
                    reading = guards.ounce_reading(source_uom, view)
                    if proposal and reading != "WEIGHT":
                        as_volume = self.group_b_rule_engine.propose(
                            product.legacy_size, guards.FLUID_OUNCE_UOM,
                        )
                        if as_volume and reading == "VOLUME":
                            proposal = as_volume
                            item["guards"].append(
                                guards.fluid_ounce_note(product.legacy_size, view.name)
                            )
                        elif as_volume:
                            item["guards"].append(guards.fluid_ounce_review(
                                product.legacy_size, proposal.standard_size,
                                as_volume.standard_size, view.name,
                            ))
                    if proposal:
                        item["guards"].extend(filter(None, (
                            guards.text_contradiction(
                                discrepancies.signals.values(), proposal.standard_size,
                                proposal.standard_uom, _decimal_or_none(product.standard_pack_size),
                            ) if discrepancies else None,
                            guards.legacy_is_pack_total(
                                discrepancies.signals.values(), proposal.standard_size,
                                proposal.standard_uom, _decimal_or_none(product.standard_pack_size),
                            ) if discrepancies else None,
                            guards.implausible_size(
                                profile, levels_of(product), proposal.standard_size,
                                proposal.standard_uom, read_from_text=False,
                            ),
                            guards.count_in_weight_category(
                                source_uom, proposal.standard_uom, view,
                            ),
                        )))
                        item["field_proposals"].update({
                            "standard_size": str(proposal.standard_size),
                            "standard_uom": proposal.standard_uom,
                        })
                        item["method"] = ProposalMethod.RULE.value
                        item["reason_code"] = "RULE_CONVERSION"
                        item["rule"] = proposal.model_dump(mode="json")
                        item["field_provenance"]["standard_size"] = self._rule_provenance(proposal.rule_id)
                        item["field_provenance"]["standard_uom"] = self._rule_provenance(proposal.rule_id)
                    else:
                        item["reason_code"] = "NO_RULE" if self.registry.get(source_uom) is None else "MALFORMED_VALUE"
                    item["review"] = self._pending_review(size_uom=True)
                pack_assessment = self.pack_size_service.assess(product)
                self._apply_pack_assessment(item, pack_assessment)
                if pack_assessment.status == PackStatus.NEEDS_AGENT:
                    if self.pack_size_inference_enabled:
                        effective_size = item["field_proposals"].get("standard_size") or product.standard_size
                        effective_uom = item["field_proposals"].get("standard_uom") or product.standard_uom
                        group_b_pack_candidates.append(
                            (len(items), product, effective_size, effective_uom)
                        )
                    else:
                        item["pack_result"]["status"] = "AGENT_DISABLED"
                        item["pack_result"]["reason_code"] = "PACK_AGENT_DISABLED"
                _log_event(
                    "item.rule_processed",
                    job_id=job_id,
                    item_no=product.item_no,
                    row_number=workbook_row.row_number,
                    route=group.value,
                    source_uom=(
                        product.raw_standard_uom if subtype == "B3" else product.legacy_uom
                    ),
                    subtype=subtype,
                    reason_code=item["reason_code"],
                    rule_id=item["rule"].get("rule_id"),
                )
            elif group == WorkGroup.C:
                group_c.append((len(items), product))
                item["review"] = self._pending_review(size_uom=True)
                self._apply_pack_assessment(item, self.pack_size_service.assess(product))
                if (
                    not self.pack_size_inference_enabled
                    and item["pack_result"]["status"] == PackStatus.NEEDS_AGENT.value
                ):
                    item["pack_result"]["status"] = "AGENT_DISABLED"
                    item["pack_result"]["reason_code"] = "PACK_AGENT_DISABLED"
                _log_event(
                    "item.inference_queued",
                    job_id=job_id,
                    item_no=product.item_no,
                    row_number=workbook_row.row_number,
                    route=group.value,
                )
            elif group == WorkGroup.INCOMPLETE:
                self._complete_partial_row(item, workbook_row, discrepancies)
            elif group == WorkGroup.VALIDATION_REVIEW:
                item["reason_code"] = "GROUP_A_VALIDATION_REVIEW"
                item["review"] = self._pending_review(size_uom=True, pack=True)
            elif group == WorkGroup.DATA_SHAPE_ERROR and validation is not None:
                item["reason_code"] = "GROUP_A_VALIDATION_INVALID"
                item["review"] = self._pending_review(size_uom=True, pack=True)
            items.append(item)

        progress.start(
            "PROCESSING_DESCRIPTIONS", len(group_b_pack_candidates) + len(group_c)
        )
        agent_calls_done = 0

        def agent_call_finished() -> None:
            nonlocal agent_calls_done
            agent_calls_done += 1
            progress.advance(agent_calls_done)

        if group_b_pack_candidates:
            asyncio.run(self._infer_group_b_pack(
                items, group_b_pack_candidates, agent_call_finished,
            ))
        if group_c:
            asyncio.run(self._infer_group_c(items, group_c, agent_call_finished, profile))

        # Materialize a stable result contract only after deterministic and
        # agent phases have finished populating proposals and provenance.
        items = [enrich_result_item(item) for item in items]

        progress.start("CHECKING_DISCREPANCIES")
        discrepancies = 0
        for item in items:
            if item["discrepancy"]["flagged"]:
                discrepancies += 1
            for detail in item["discrepancy"]["details"]:
                if detail["status"] == "CONFLICT":
                    counts[f"discrepancy_{detail['scope'].lower()}_{detail['aspect'].lower()}"] += 1
        counts["discrepancies"] = discrepancies
        for item in items:
            pack_result = item.get("pack_result") or {}
            status = str(pack_result.get("status") or "NOT_EVALUATED").lower()
            counts[f"pack_{status}"] += 1
            if pack_result.get("invalid_existing"):
                counts["pack_invalid_existing"] += 1
            if item["route"] in {WorkGroup.B.value, WorkGroup.C.value}:
                counts[f"route_{item['route'].lower()}_pack_{status}"] += 1
            counts[f"outcome_{outcome_group(item).lower()}"] += 1
        stats = {
            "workbook_rows": counts["workbook_rows"],
            "department_rows": counts["department_rows"],
            "purged": counts["purged"],
            "live": counts["live"],
            # The outcome groups, decided from each row's final status.
            "groups": {group: counts[f"outcome_{group.lower()}"] for group in GROUPS},
            # The routes: which method the tool used on each row.
            "route_a": counts["route_a"],
            "route_b": counts["route_b"],
            "route_b1": counts["route_b1"],
            "route_b2": counts["route_b2"],
            "route_b3": counts["route_b3"],
            "route_c": counts["route_c"],
            "route_incomplete": counts["route_incomplete"],
            "validation_review": counts["route_validation_review"],
            "route_a_validation_warnings": counts["route_a_validation_warnings"],
            "data_shape_error": counts["route_data_shape_error"],
            "discrepancies": discrepancies,
            "discrepancy_bilingual_measurement_conflicts": counts["discrepancy_bilingual_pair_measurement"],
            "discrepancy_bilingual_count_conflicts": counts["discrepancy_bilingual_pair_count"],
            "pack_existing_valid": counts["pack_existing_valid"],
            "pack_normalized_existing": counts["pack_normalize_existing"],
            "pack_deterministic_proposed": counts["pack_deterministic_proposal"],
            "pack_agent_proposed": counts["pack_agent_proposal"],
            "pack_agent_declined": counts["pack_agent_declined"],
            "pack_conflict": counts["pack_conflict"],
            "pack_not_found": counts["pack_not_found"],
            "pack_agent_error": counts["pack_agent_error"],
            "pack_agent_disabled": counts["pack_agent_disabled"],
            "pack_invalid_existing": counts["pack_invalid_existing"],
            "route_b_pack_deterministic_proposed": counts["route_b_pack_deterministic_proposal"],
            "route_b_pack_agent_proposed": counts["route_b_pack_agent_proposal"],
            "route_c_pack_deterministic_proposed": counts["route_c_pack_deterministic_proposal"],
            "route_c_pack_agent_proposed": counts["route_c_pack_agent_proposal"],
        }
        if job.get("snapshot_label") == "v0.2":
            differences = {key: (stats[key], expected) for key, expected in EXPECTED_V02.items() if stats[key] != expected}
            if differences:
                raise ValueError(f"v0.2 profile invariant failed: {differences}")

        progress.start("SAVING_RESULTS", len(items))
        for offset in range(0, len(items), 1000):
            self.repositories.replace_items(items[offset:offset + 1000])
            progress.advance(min(offset + 1000, len(items)))
        covered = sorted(source for source in source_counts if self.registry.get(source))
        uncovered = sorted(source for source in source_counts if not self.registry.get(source))
        readiness = {
            "ruleset_version": self.registry.version,
            "ruleset_checksum": self.registry.checksum,
            "covered_source_uoms": covered,
            "uncovered_source_uoms": uncovered,
            "uncovered_affected_rows": sum(source_counts[source] for source in uncovered),
        }
        validation_policy = {
            "version": GROUP_A_VALIDATION_VERSION,
            "alias_version": STANDARD_UOM_ALIAS_VERSION,
            "alias_checksum": STANDARD_UOM_ALIAS_CHECKSUM,
            "group_b_rounding": "EXCEL_NEAREST_WHOLE",
            "pack_extraction_version": PACK_EXTRACTION_VERSION,
            "discrepancy_engine_version": DISCREPANCY_ENGINE_VERSION,
            "guards_version": guards.GUARDS_VERSION,
            "category_profile": profile.as_dict(),
            "packaging_expression_version": PACKAGING_EXPRESSION_VERSION,
            "pack_agent_fallback_enabled": self.pack_size_inference_enabled,
        }
        self.repositories.update_job(job_id, {
            "status": "READY_FOR_REVIEW",
            "stats": stats,
            "validation_policy": validation_policy,
            "rule_readiness": readiness,
            "ai_usage": dict(self.ai_usage),
            "quality": self.quality_service.build_report(
                {**job, "validation_policy": validation_policy}, items,
            ),
            "progress": {
                "stage": "READY_FOR_REVIEW", "processed": stats["live"],
                "total": stats["live"], "unit": "ROWS", "percent": 100,
            },
        })
        _log_event(
            "job.processing_completed",
            job_id=job_id,
            duration_ms=round((perf_counter() - started) * 1000),
            **stats,
        )

    def _base_item(
        self,
        job_id: str,
        row: WorkbookRow,
        group: WorkGroup,
        validation: GroupAValidationResult | None = None,
        discrepancies: DiscrepancyReport | None = None,
    ) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "row_number": row.row_number,
            "item_no": row.product.item_no,
            "department": row.product.department,
            "route": group.value,
            "context": _context(row.product),
            "original": _original(row.product),
            "field_proposals": {"standard_size": None, "standard_uom": None, "standard_pack_size": None},
            "method": ProposalMethod.NONE.value,
            "reason_code": "VALIDATED_BASE_UNIT" if group == WorkGroup.A else None,
            "rule": {"rule_id": None, "factor": None, "source_uom": None, "target_uom": None},
            "field_provenance": {
                "standard_size": {"method": "EXISTING" if group == WorkGroup.A else "NONE"},
                "standard_uom": {"method": "EXISTING" if group == WorkGroup.A else "NONE"},
                "standard_pack_size": {"method": "EXISTING" if group == WorkGroup.A else "NONE"},
            },
            "pack_result": None,
            "evidence": [],
            "confidence": None,
            "validation": validation.as_dict() if validation else None,
            "guards": [],
            "discrepancy": (
                discrepancies.as_dict() if discrepancies
                else {"version": DISCREPANCY_ENGINE_VERSION, "flagged": False, "details": [], "signals": {}}
            ),
            "review": self._not_required_review(),
        }

    def _complete_partial_row(
        self, item: dict[str, Any], row: WorkbookRow, discrepancies: DiscrepancyReport | None,
    ) -> None:
        """Some of size, unit and pack are filled. Keep them, complete the rest from the old
        size or the description, then run the usual checks on the completed values."""
        product = row.product
        assessment = self.pack_size_service.assess(product)
        item["pack_result"] = assessment.as_dict()
        fill = self.gap_filler.fill(product, assessment)
        item["field_proposals"].update(fill.proposals)
        item["field_provenance"].update(fill.provenance)
        item["guards"].extend(fill.issues)
        item["reason_code"] = "PARTLY_FILLED_ROW"
        if fill.proposals:
            item["method"] = ProposalMethod.RULE.value
        if fill.check_values is not None:
            size, unit, pack = fill.check_values
            completed = product.model_copy(update={
                "standard_size": size, "standard_uom": unit, "standard_pack_size": pack,
                "raw_standard_size": size, "raw_standard_uom": unit, "raw_standard_pack_size": pack,
            })
            raw = dict(row.raw)
            raw.update({FIELD_MAP["standard_size"]: size, FIELD_MAP["standard_uom"]: unit,
                        FIELD_MAP["standard_pack_size"]: pack})
            checked = self.group_a_validator.validate(
                WorkbookRow(row_number=row.row_number, raw=raw, product=completed),
                discrepancies=discrepancies,
            )
            if checked is not None:
                item["validation"] = checked.as_dict()

    def _record_usage(self, response: Any) -> None:
        self.ai_usage["calls"] += 1
        self.ai_usage["input_tokens"] += response.metadata.input_tokens or 0
        self.ai_usage["output_tokens"] += response.metadata.output_tokens or 0

    @staticmethod
    def _not_required_review() -> dict[str, Any]:
        return {
            "field_decisions": {"standard_size": "NOT_REQUIRED", "standard_uom": "NOT_REQUIRED", "standard_pack_size": "NOT_REQUIRED"},
            "overall_status": "NOT_REQUIRED", "override_values": None, "comment": None,
        }

    @staticmethod
    def _pending_review(size_uom: bool, pack: bool = False) -> dict[str, Any]:
        return {
            "field_decisions": {
                "standard_size": "PENDING" if size_uom else "NOT_REQUIRED",
                "standard_uom": "PENDING" if size_uom else "NOT_REQUIRED",
                "standard_pack_size": "PENDING" if pack else "NOT_REQUIRED",
            },
            "overall_status": "PENDING", "override_values": None, "comment": None,
        }

    @staticmethod
    def _rule_provenance(rule_id: str) -> dict[str, str]:
        return {"method": ProposalMethod.RULE.value, "rule_id": rule_id}

    def _apply_pack_assessment(self, item: dict[str, Any], assessment: PackAssessment) -> None:
        item["pack_result"] = assessment.as_dict()
        if assessment.status == PackStatus.EXISTING_VALID:
            item["field_provenance"]["standard_pack_size"] = {"method": "EXISTING"}
            return
        if assessment.status not in {
            PackStatus.NORMALIZE_EXISTING,
            PackStatus.DETERMINISTIC_PROPOSAL,
        }:
            return
        if assessment.pack_size is None:
            return
        item["field_proposals"]["standard_pack_size"] = str(
            assessment.pack_size.quantize(Decimal("1"))
        )
        item["field_provenance"]["standard_pack_size"] = {
            "method": ProposalMethod.RULE.value,
            "rule_id": assessment.pattern_id or "PACK_VALUE_NORMALIZATION",
            "evidence": assessment.evidence.model_dump() if assessment.evidence else None,
        }
        if assessment.evidence:
            item["evidence"].append(assessment.evidence.model_dump())
        if item["method"] == ProposalMethod.NONE.value:
            item["method"] = ProposalMethod.RULE.value
        item["review"]["field_decisions"]["standard_pack_size"] = "PENDING"

    async def _infer_group_b_pack(
        self,
        items: list[dict[str, Any]],
        candidates: list[tuple[int, InputProduct, Decimal | str | None, str | None]],
        on_done: Callable[[], None] = lambda: None,
    ) -> None:
        semaphore = asyncio.Semaphore(self.ai_max_concurrency)

        async def infer(
            index: int,
            product: InputProduct,
            standard_size: Decimal | str | None,
            standard_uom: str | None,
        ) -> None:
            request = self.pack_size_service.agent_request(
                product, standard_size, standard_uom
            )
            started = perf_counter()
            try:
                _log_event(
                    "item.pack_inference_started",
                    job_id=items[index]["job_id"],
                    item_no=product.item_no,
                    row_number=items[index]["row_number"],
                    route=items[index]["route"],
                )
                async with semaphore:
                    response = await self.inference_provider.infer(request)
                result = response.result
                validate_evidence(request, result)
                self._record_usage(response)
                item = items[index]
                item["pack_ai_raw"] = result.model_dump(mode="json")
                if (
                    result.status == "PACK_PROPOSAL"
                    and result.pack_size is not None
                    and guards.is_sellable_pack(result)
                ):
                    item["field_proposals"]["standard_pack_size"] = str(
                        result.pack_size.quantize(Decimal("1"))
                    )
                    item["review"]["field_decisions"]["standard_pack_size"] = "PENDING"
                    item["pack_result"] = {
                        "version": PACK_EXTRACTION_VERSION,
                        "status": "AGENT_PROPOSAL",
                        "reason_code": result.reason_code,
                        "pack_size": str(result.pack_size.quantize(Decimal("1"))),
                        "evidence": result.pack_evidence.model_dump(),
                        "invalid_existing": item["pack_result"].get("invalid_existing", False),
                    }
                    item["field_provenance"]["standard_pack_size"] = {
                        "method": ProposalMethod.AI_INFERENCE.value,
                        "evidence": result.pack_evidence.model_dump(),
                        "confidence": result.confidence,
                        "provider": response.metadata.model_dump(mode="json"),
                    }
                    item["evidence"].append(result.pack_evidence.model_dump())
                    if not guards.pack_count_is_settled(
                        result.pack_size, item["original"].get("legacy_size"), item["original"].get("legacy_uom"),
                    ):
                        item["guards"].append(guards.ai_pack_needs_confirmation(
                            result.pack_size, result.pack_evidence.fragment,
                        ))
                else:
                    item["pack_result"]["status"] = "AGENT_DECLINED"
                    item["pack_result"]["reason_code"] = (
                        f"PACK_COUNT_IS_{result.pack_role}"
                        if result.pack_size is not None and not guards.is_sellable_pack(result)
                        else result.reason_code
                    )
                _log_event(
                    "item.pack_inference_completed",
                    job_id=item["job_id"],
                    item_no=product.item_no,
                    row_number=item["row_number"],
                    status=result.status,
                    reason_code=result.reason_code,
                    duration_ms=round((perf_counter() - started) * 1000),
                )
            except Exception as exc:
                item = items[index]
                item["pack_result"]["status"] = "AGENT_ERROR"
                item["pack_result"]["reason_code"] = "PACK_AGENT_ERROR"
                item["pack_result"]["error"] = f"{type(exc).__name__}: {exc}"
                if isinstance(exc, InvalidInferenceResponseError):
                    item["pack_result"]["validation_attempts"] = [
                        {"attempt": attempt, "error": error}
                        for attempt, error in enumerate(exc.validation_errors, start=1)
                    ]
                _log_event(
                    "item.pack_inference_failed",
                    job_id=item["job_id"],
                    item_no=product.item_no,
                    row_number=item["row_number"],
                    error_type=type(exc).__name__,
                    error=str(exc),
                    duration_ms=round((perf_counter() - started) * 1000),
                )

        async def tracked(*candidate: Any) -> None:
            try:
                await infer(*candidate)
            finally:
                on_done()

        await asyncio.gather(*(tracked(*candidate) for candidate in candidates))

    async def _infer_group_c(
        self,
        items: list[dict[str, Any]],
        candidates: list[tuple[int, InputProduct]],
        on_done: Callable[[], None] = lambda: None,
        profile: CategoryProfile | None = None,
    ) -> None:
        profile = profile or CategoryProfile()
        semaphore = asyncio.Semaphore(self.ai_max_concurrency)

        async def infer(index: int, product: InputProduct) -> None:
            started = perf_counter()
            request = InferenceRequest(
                item_brand_eng=product.item_brand_eng,
                item_brand_local_lang=product.item_brand_local,
                item_desc_eng=product.item_desc_eng,
                item_desc_local_lang=product.item_desc_local,
                web_description_eng=product.web_description_eng,
                web_description_chi=product.web_description_chi,
                # D3: category context only; never citable as evidence.
                division=product.division,
                category=product.category,
                subcategory=product.subcategory,
                section=product.section,
            )
            try:
                _log_event(
                    "item.inference_started",
                    job_id=items[index]["job_id"],
                    item_no=product.item_no,
                    row_number=items[index]["row_number"],
                    route=items[index]["route"],
                )
                async with semaphore:
                    response = await self.inference_provider.infer(request)
                result = response.result
                validate_evidence(request, result)
                item = items[index]
                item["method"] = ProposalMethod.AI_INFERENCE.value
                item["reason_code"] = result.reason_code
                item["confidence"] = result.confidence
                item["ai_provenance"] = response.metadata.model_dump(mode="json")
                # The complete answer is kept, including what the agent saw and set aside.
                item["ai_raw"] = result.model_dump(mode="json")
                self._record_usage(response)
                observations = []
                if result.measurement:
                    observations.append(result.measurement)
                observations.extend(result.conflicting_measurements)
                item["evidence"].extend([
                    {"field": observed.field, "fragment": observed.fragment}
                    for observed in observations
                ])
                if result.status == "PROPOSAL" and result.measurement:
                    item["ai_observation"] = result.measurement.model_dump(mode="json")
                    proposal = self.rule_engine.propose(
                        result.measurement.value,
                        result.measurement.uom,
                    )
                    if not result.measurement.describes_product_size:
                        # G1: a capacity, dimension, name or grade never becomes a size,
                        # whatever the model recommended.
                        proposal = None
                        item["reason_code"] = "AI_MEASUREMENT_NOT_PRODUCT_SIZE"
                        item["guards"].append(guards.not_a_product_size(result.measurement))
                    elif proposal is None:
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
                        shared_provenance = {
                            "method": "AI_INFERENCE+RULE",
                            "rule_id": proposal.rule_id,
                            "evidence": result.measurement.model_dump(mode="json"),
                            "provider": response.metadata.model_dump(mode="json"),
                        }
                        item["field_provenance"]["standard_size"] = shared_provenance
                        item["field_provenance"]["standard_uom"] = shared_provenance
                        implausible = guards.implausible_size(
                            profile, levels_of(product), proposal.standard_size,
                            proposal.standard_uom, read_from_text=True,
                        )
                        if implausible:
                            item["guards"].append(implausible)
                        item["confidence"] = guards.derived_confidence(
                            result.measurement, request, blocked=bool(implausible),
                        )
                # A pack count may be written even when no size is ("SMALL CAN BEER 4'S").
                pack_status = (item.get("pack_result") or {}).get("status")
                if (
                    self.pack_size_inference_enabled
                    and result.pack_size is not None
                    and guards.is_sellable_pack(result)
                    and pack_status in {
                        PackStatus.NEEDS_AGENT.value,
                        PackStatus.NOT_FOUND.value,
                    }
                ):
                    item["field_proposals"]["standard_pack_size"] = str(
                        result.pack_size.quantize(Decimal("1"))
                    )
                    item["review"]["field_decisions"]["standard_pack_size"] = "PENDING"
                    item["pack_result"] = {
                        "version": PACK_EXTRACTION_VERSION,
                        "status": "AGENT_PROPOSAL",
                        "reason_code": "EXPLICIT_PACK_COUNT",
                        "pack_size": str(result.pack_size.quantize(Decimal("1"))),
                        "evidence": result.pack_evidence.model_dump(),
                        "invalid_existing": item["pack_result"].get("invalid_existing", False),
                    }
                    item["field_provenance"]["standard_pack_size"] = {
                        "method": ProposalMethod.AI_INFERENCE.value,
                        "evidence": result.pack_evidence.model_dump(),
                        "confidence": result.confidence,
                        "provider": response.metadata.model_dump(mode="json"),
                    }
                    item["evidence"].append(result.pack_evidence.model_dump())
                    if not guards.pack_count_is_settled(
                        result.pack_size, item["original"].get("legacy_size"), item["original"].get("legacy_uom"),
                    ):
                        item["guards"].append(guards.ai_pack_needs_confirmation(
                            result.pack_size, result.pack_evidence.fragment,
                        ))
                pack_status = (item.get("pack_result") or {}).get("status")
                if (
                    self.pack_size_inference_enabled
                    and result.pack_size is None
                    and pack_status in {
                        PackStatus.NEEDS_AGENT.value,
                        PackStatus.NOT_FOUND.value,
                    }
                ):
                    item["pack_result"]["status"] = "AGENT_DECLINED"
                    item["pack_result"]["reason_code"] = "PACK_SIZE_NOT_PROPOSED"
                _log_event(
                    "item.inference_completed",
                    job_id=item["job_id"],
                    item_no=product.item_no,
                    row_number=item["row_number"],
                    route=item["route"],
                    status=result.status,
                    reason_code=item["reason_code"],
                    model_id=response.metadata.model_id,
                    latency_ms=response.metadata.latency_ms,
                    duration_ms=round((perf_counter() - started) * 1000),
                )
            except InvalidInferenceResponseError as exc:
                items[index]["reason_code"] = "AI_INVALID_RESPONSE"
                items[index]["ai_error"] = str(exc)
                items[index]["ai_attempts"] = [
                    {"attempt": attempt, "error": error}
                    for attempt, error in enumerate(exc.validation_errors, start=1)
                ]
                if (items[index].get("pack_result") or {}).get("status") in {
                    PackStatus.NEEDS_AGENT.value, PackStatus.NOT_FOUND.value,
                }:
                    items[index]["pack_result"]["status"] = "AGENT_ERROR"
                    items[index]["pack_result"]["reason_code"] = "PACK_AGENT_ERROR"
                _log_event(
                    "item.inference_failed",
                    job_id=items[index]["job_id"],
                    item_no=product.item_no,
                    row_number=items[index]["row_number"],
                    reason_code="AI_INVALID_RESPONSE",
                    error=str(exc),
                    duration_ms=round((perf_counter() - started) * 1000),
                )
            except ValueError as exc:
                items[index]["reason_code"] = "AI_INVALID_RESPONSE"
                items[index]["ai_error"] = str(exc)
                if (items[index].get("pack_result") or {}).get("status") in {
                    PackStatus.NEEDS_AGENT.value, PackStatus.NOT_FOUND.value,
                }:
                    items[index]["pack_result"]["status"] = "AGENT_ERROR"
                    items[index]["pack_result"]["reason_code"] = "PACK_AGENT_ERROR"
                _log_event(
                    "item.inference_failed",
                    job_id=items[index]["job_id"],
                    item_no=product.item_no,
                    row_number=items[index]["row_number"],
                    reason_code="AI_INVALID_RESPONSE",
                    error=str(exc),
                    duration_ms=round((perf_counter() - started) * 1000),
                )
            except Exception as exc:
                items[index]["reason_code"] = "AI_PROVIDER_ERROR"
                items[index]["ai_error"] = str(exc)
                if (items[index].get("pack_result") or {}).get("status") in {
                    PackStatus.NEEDS_AGENT.value, PackStatus.NOT_FOUND.value,
                }:
                    items[index]["pack_result"]["status"] = "AGENT_ERROR"
                    items[index]["pack_result"]["reason_code"] = "PACK_AGENT_ERROR"
                _log_event(
                    "item.inference_failed",
                    job_id=items[index]["job_id"],
                    item_no=product.item_no,
                    row_number=items[index]["row_number"],
                    reason_code="AI_PROVIDER_ERROR",
                    error=str(exc),
                    duration_ms=round((perf_counter() - started) * 1000),
                )

        async def tracked(*candidate: Any) -> None:
            try:
                await infer(*candidate)
            finally:
                on_done()

        await asyncio.gather(*(tracked(*candidate) for candidate in candidates))
