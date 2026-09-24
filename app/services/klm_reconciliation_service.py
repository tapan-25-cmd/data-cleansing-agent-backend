from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from app.services.discrepancy_service import within_conversion_tolerance
from app.services.rule_engine import RuleEngine


KLM_RECONCILIATION_VERSION = "klm-reconciliation-v3"


class LegacyRelationship(str, Enum):
    NOT_AVAILABLE = "NOT_AVAILABLE"
    NOT_COMPARABLE = "NOT_COMPARABLE"
    UOM_MISMATCH = "UOM_MISMATCH"
    UNIT_MATCH = "UNIT_MATCH"
    UNIT_ROUNDING_MATCH = "UNIT_ROUNDING_MATCH"
    TOTAL_MATCH = "TOTAL_MATCH"
    TOTAL_ROUNDING_MATCH = "TOTAL_ROUNDING_MATCH"
    SIGNIFICANT_MISMATCH = "SIGNIFICANT_MISMATCH"


@dataclass(frozen=True)
class LegacyAssessment:
    relationship: LegacyRelationship
    expected_value: Decimal | None = None
    expected_uom: str | None = None
    existing_unit_value: Decimal | None = None
    existing_pack_size: Decimal | None = None
    existing_total: Decimal | None = None
    exact_identity: bool = False

    @property
    def legacy_looks_like_total(self) -> bool:
        return self.relationship in {
            LegacyRelationship.TOTAL_MATCH,
            LegacyRelationship.TOTAL_ROUNDING_MATCH,
        }

    def as_dict(self) -> dict[str, str | bool | None]:
        return {
            "version": KLM_RECONCILIATION_VERSION,
            "relationship": self.relationship.value,
            "expected_value": _plain(self.expected_value),
            "expected_uom": self.expected_uom,
            "existing_unit_value": _plain(self.existing_unit_value),
            "existing_pack_size": _plain(self.existing_pack_size),
            "existing_total": _plain(self.existing_total),
            "exact_identity": self.exact_identity,
        }


def _plain(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


class KLMReconciliationService:
    """Classify legacy evidence against a complete K/L/M tuple.

    The service does not choose a packaging split. It prevents a whole-pack total
    from being treated as a replacement for K while retaining M.
    """

    def __init__(self, comparison_engine: RuleEngine):
        self.comparison_engine = comparison_engine

    def assess_legacy(
        self,
        *,
        legacy_size: object | None,
        legacy_uom: str | None,
        standard_size: Decimal,
        standard_uom: str,
        standard_pack_size: Decimal,
    ) -> LegacyAssessment:
        total = standard_size * standard_pack_size
        if legacy_size is None or legacy_uom is None or not str(legacy_uom).strip():
            return LegacyAssessment(
                LegacyRelationship.NOT_AVAILABLE,
                existing_unit_value=standard_size,
                existing_pack_size=standard_pack_size,
                existing_total=total,
            )

        proposal = self.comparison_engine.propose(legacy_size, legacy_uom)
        if proposal is None:
            return LegacyAssessment(
                LegacyRelationship.NOT_COMPARABLE,
                existing_unit_value=standard_size,
                existing_pack_size=standard_pack_size,
                existing_total=total,
            )

        identity = (
            proposal.source_uom == proposal.target_uom
            and proposal.factor == Decimal("1")
        )
        expected = proposal.raw_target if identity else proposal.standard_size
        base = dict(
            expected_value=expected,
            expected_uom=proposal.standard_uom,
            existing_unit_value=standard_size,
            existing_pack_size=standard_pack_size,
            existing_total=total,
            exact_identity=identity,
        )
        if proposal.standard_uom != standard_uom:
            return LegacyAssessment(LegacyRelationship.UOM_MISMATCH, **base)
        if expected == standard_size:
            return LegacyAssessment(LegacyRelationship.UNIT_MATCH, **base)
        if standard_pack_size != Decimal("1") and expected == total:
            return LegacyAssessment(LegacyRelationship.TOTAL_MATCH, **base)
        # A count is exact or it is not: 1 PC against 2 EA is a disagreement, not
        # label rounding. The allowance exists for converted weights and volumes.
        counts = standard_uom == "EA"
        if not identity and not counts and within_conversion_tolerance(expected, standard_size):
            return LegacyAssessment(LegacyRelationship.UNIT_ROUNDING_MATCH, **base)
        # The exact-match requirement for same-unit legacies protects the unit
        # comparison (4.5 GM must not become a 5 GM mismatch). The whole-pack
        # comparison is a different question: a legacy 380 GM against 63.4 GM × 6
        # = 380.4 GM is the pack weight printed rounded, whatever the unit.
        if (
            standard_pack_size != Decimal("1")
            and not counts
            and within_conversion_tolerance(expected, total)
        ):
            return LegacyAssessment(LegacyRelationship.TOTAL_ROUNDING_MATCH, **base)
        return LegacyAssessment(LegacyRelationship.SIGNIFICANT_MISMATCH, **base)

