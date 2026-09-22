from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from enum import Enum
import re

from app.agents.provider import Evidence, InferenceRequest, KnownMeasurement
from app.domain.product import InputProduct
from app.services.normalization import blank


PACK_EXTRACTION_VERSION = "pack-extraction-v2"
PACK_FIELDS = (
    ("item_desc_eng", "item_desc_eng"),
    ("item_desc_local_lang", "item_desc_local"),
    ("web_description_eng", "web_description_eng"),
    ("web_description_chi", "web_description_chi"),
)


class PackStatus(str, Enum):
    EXISTING_VALID = "EXISTING_VALID"
    NORMALIZE_EXISTING = "NORMALIZE_EXISTING"
    DETERMINISTIC_PROPOSAL = "DETERMINISTIC_PROPOSAL"
    CONFLICT = "CONFLICT"
    NEEDS_AGENT = "NEEDS_AGENT"
    NOT_FOUND = "NOT_FOUND"


@dataclass(frozen=True)
class PackCandidate:
    pack_size: Decimal
    evidence: Evidence
    pattern_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "pack_size": str(self.pack_size),
            "evidence": self.evidence.model_dump(),
            "pattern_id": self.pattern_id,
        }


@dataclass(frozen=True)
class PackAssessment:
    status: PackStatus
    reason_code: str
    pack_size: Decimal | None = None
    evidence: Evidence | None = None
    pattern_id: str | None = None
    invalid_existing: bool = False
    candidates: tuple[PackCandidate, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "version": PACK_EXTRACTION_VERSION,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "pack_size": str(self.pack_size) if self.pack_size is not None else None,
            "evidence": self.evidence.model_dump() if self.evidence else None,
            "pattern_id": self.pattern_id,
            "invalid_existing": self.invalid_existing,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
        }


# The unit ends at a word boundary, or at a glued multiplier such as ``190GMX3``.
_MEASUREMENT = r"\d+(?:\.\d+)?\s*(?:ml|millilit(?:er|re)s?|l|lt|lit(?:er|re)s?|g|gm|grams?|kg|oz|lb|ft)(?:\b|(?=[x×]\s*\d))"
_PATTERNS = (
    ("COUNT_X_MEASUREMENT", re.compile(rf"(?<![\w.])(?P<count>\d+)\s*(?:x|×)\s*{_MEASUREMENT}", re.IGNORECASE)),
    ("MEASUREMENT_X_COUNT", re.compile(rf"{_MEASUREMENT}\s*(?:x|×)\s*(?P<count>\d+)(?![\w.])", re.IGNORECASE)),
    ("PACK_OF_COUNT", re.compile(r"\b(?:pack|case|carton|box)\s+of\s+(?P<count>\d+)(?![\d.])", re.IGNORECASE)),
    ("COUNT_PACK", re.compile(
        r"(?<![\w.])(?P<count>\d+)\s*(?:pack|pk|packs?|count|ct)\b",
        re.IGNORECASE,
    )),
    ("COUNT_CONTAINER_MEASUREMENT", re.compile(
        rf"(?<![\w.])(?P<count>\d+)\s*(?:pcs?|pieces?|cans?|bottles?|units?)\b\s*(?:x|of)?\s*{_MEASUREMENT}",
        re.IGNORECASE,
    )),
)
# D1: a pack word alone ("TORTILLA (8 PACK)" 320 GM) says how many pieces are inside,
# not that each piece is a sellable unit with its own size. It becomes a pack size only
# when the text also gives the per-piece structure ("4 PK x 250 GM", "3PK (190GMX3)").
_WORD_PATTERNS = frozenset({"PACK_OF_COUNT", "COUNT_PACK"})
_FOLLOWED_BY_SIZE = re.compile(rf"\s*(?:x|×)\s*{_MEASUREMENT}", re.IGNORECASE)
_PACK_HINT = re.compile(
    r"(?:\d\s*(?:x|×)|(?:x|×)\s*\d|\d+\s*(?:pcs?|pieces?|cans?|bottles?|units?)\b|\b(?:pack|pk|case|carton|box|pcs?|pieces?|cans?|bottles?|units?|count|ct)\b|\d+\s*['’]s\b)",
    re.IGNORECASE,
)


def _positive_whole(value: object) -> Decimal | None:
    if blank(value) or isinstance(value, (bool, date, datetime, time)):
        return None
    if isinstance(value, str) and (value.strip().startswith("=") or value.strip().startswith("#")):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    if not parsed.is_finite() or parsed <= 0 or parsed != parsed.to_integral_value():
        return None
    return parsed


class PackSizeService:
    """Resolve pack size from existing M, deterministic text, or an agent handoff."""

    def assess(self, product: InputProduct) -> PackAssessment:
        raw_pack = product.raw_standard_pack_size
        existing = _positive_whole(raw_pack)
        if existing is not None:
            status = PackStatus.NORMALIZE_EXISTING if isinstance(raw_pack, str) else PackStatus.EXISTING_VALID
            return PackAssessment(
                status=status,
                reason_code=status.value,
                pack_size=existing,
            )

        invalid_existing = not blank(raw_pack)
        candidates: list[PackCandidate] = []
        word_only: list[PackCandidate] = []
        has_hint = False
        for evidence_field, product_attribute in PACK_FIELDS:
            text = getattr(product, product_attribute)
            if not text:
                continue
            has_hint = has_hint or bool(_PACK_HINT.search(text))
            for pattern_id, pattern in _PATTERNS:
                for match in pattern.finditer(text):
                    pack_size = _positive_whole(match.group("count"))
                    if pack_size is None:
                        continue
                    candidate = PackCandidate(
                        pack_size=pack_size,
                        evidence=Evidence(field=evidence_field, fragment=match.group(0)),
                        pattern_id=pattern_id,
                    )
                    if pattern_id in _WORD_PATTERNS and not _FOLLOWED_BY_SIZE.match(text, match.end()):
                        word_only.append(candidate)
                    else:
                        candidates.append(candidate)
        # Pack words corroborate or contradict a structure, but never stand alone.
        if candidates:
            candidates.extend(word_only)

        distinct = {candidate.pack_size for candidate in candidates}
        if len(distinct) == 1:
            selected = candidates[0]
            return PackAssessment(
                status=PackStatus.DETERMINISTIC_PROPOSAL,
                reason_code="EXPLICIT_PACK_PATTERN",
                pack_size=selected.pack_size,
                evidence=selected.evidence,
                pattern_id=selected.pattern_id,
                invalid_existing=invalid_existing,
                candidates=tuple(candidates),
            )
        if len(distinct) > 1:
            return PackAssessment(
                status=PackStatus.CONFLICT,
                reason_code="PACK_SIZE_CONFLICT",
                invalid_existing=invalid_existing,
                candidates=tuple(candidates),
            )
        if has_hint or word_only:
            return PackAssessment(
                status=PackStatus.NEEDS_AGENT,
                reason_code=(
                    "PACK_WORD_WITHOUT_PIECE_SIZE" if word_only else "PACK_PATTERN_AMBIGUOUS"
                ),
                invalid_existing=invalid_existing,
                candidates=tuple(word_only),
            )
        return PackAssessment(
            status=PackStatus.NOT_FOUND,
            reason_code="PACK_SIZE_NOT_FOUND",
            invalid_existing=invalid_existing,
        )

    @staticmethod
    def agent_request(
        product: InputProduct,
        standard_size: Decimal | str | None,
        standard_uom: str | None,
    ) -> InferenceRequest:
        known = None
        if standard_size is not None and standard_uom:
            try:
                known = KnownMeasurement(value=Decimal(str(standard_size)), uom=standard_uom)
            except (InvalidOperation, ValueError):
                known = None
        return InferenceRequest(
            task="PACK_ONLY",
            known_measurement=known,
            item_desc_eng=product.item_desc_eng,
            item_desc_local_lang=product.item_desc_local,
            web_description_eng=product.web_description_eng,
            web_description_chi=product.web_description_chi,
            division=product.division,
            category=product.category,
            subcategory=product.subcategory,
            section=product.section,
        )
