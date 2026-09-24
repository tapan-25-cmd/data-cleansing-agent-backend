from decimal import Decimal
import re
from typing import Literal, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ALLOWED_EVIDENCE_FIELDS = frozenset({
    "item_brand_eng",
    "item_brand_local_lang",
    "item_desc_eng",
    "item_desc_local_lang",
    "web_description_eng",
    "web_description_chi",
})

AllowedEvidenceField: TypeAlias = Literal[
    "item_brand_eng",
    "item_brand_local_lang",
    "item_desc_eng",
    "item_desc_local_lang",
    "web_description_eng",
    "web_description_chi",
]

# What a measurement in the text describes. Only the first two can become a product size;
# the backend enforces that whatever the model recommends (guard G1).
MeasurementRole: TypeAlias = Literal[
    "NET_CONTENT_UNIT",      # amount of product in one piece
    "NET_CONTENT_TOTAL",     # amount of product in the whole sellable item
    "CAPACITY_OR_RANGE",     # what a container, tool or appliance holds or measures
    "DIMENSION",             # length, diameter, thickness
    "NAME_OR_GRADE",         # part of a name, grade, recipe or nutrition claim
    "UNCLEAR",
]
PackRole: TypeAlias = Literal["SELLABLE_PACK", "CONTENTS", "OUTER_CASE", "UNCLEAR"]
PairName: TypeAlias = Literal["ITEM_DESCRIPTION", "WEB_DESCRIPTION"]
PairStatus: TypeAlias = Literal["SUPPORTED", "INSUFFICIENT", "CONFLICT", "AMBIGUOUS"]
RelationshipType: TypeAlias = Literal[
    "AMOUNT_PER_UNIT",
    "UNITS_PER_PACK",
    "INNER_PACKS_PER_CASE",
    "STATED_TOTAL",
    "CONTAINS",
    "ALTERNATIVE",
]
PRODUCT_SIZE_ROLES = frozenset({"NET_CONTENT_UNIT", "NET_CONTENT_TOTAL"})

_REASON_FOR_STATUS = {
    "PROPOSAL": "EXPLICIT_MEASUREMENT",
    "PACK_PROPOSAL": "EXPLICIT_PACK_COUNT",
    "NOT_IN_DESCRIPTION": "NOT_IN_DESCRIPTION",
    "AMBIGUOUS": "AMBIGUOUS_DESCRIPTION",
    "CONFLICT": "DESCRIPTION_CONFLICT",
}

AgentReasonCode: TypeAlias = Literal[
    "EXPLICIT_MEASUREMENT",
    "EXPLICIT_PACK_COUNT",
    "NOT_IN_DESCRIPTION",
    "AMBIGUOUS_DESCRIPTION",
    "DESCRIPTION_CONFLICT",
]


class KnownMeasurement(BaseModel):
    value: Decimal
    uom: str = Field(min_length=1, max_length=20)

    @field_validator("uom")
    @classmethod
    def normalize_uom(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("value")
    @classmethod
    def require_positive_value(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("known measurement value must be positive and finite")
        return value


class InferenceProviderError(RuntimeError):
    pass


class InvalidInferenceResponseError(InferenceProviderError):
    def __init__(self, message: str, *, validation_errors: list[str] | None = None):
        super().__init__(message)
        self.validation_errors = tuple(validation_errors or [message])


class InferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task: Literal["MEASUREMENT_AND_PACK", "PACK_ONLY"] = "MEASUREMENT_AND_PACK"
    known_measurement: KnownMeasurement | None = None
    item_brand_eng: str | None = None
    item_brand_local_lang: str | None = None
    item_desc_eng: str | None = None
    item_desc_local_lang: str | None = None
    web_description_eng: str | None = None
    web_description_chi: str | None = None
    # D3: category context. It helps the agent tell a tool from a food, but it is never
    # evidence: a fragment may only be cited from the six text fields above. Columns B/C,
    # legacy values and the standardized fields have no place in this request.
    division: str | None = None
    category: str | None = None
    subcategory: str | None = None
    section: str | None = None
    # Provider-owned retry context. It is never workbook evidence and is absent
    # from normal requests. ADK input validation requires retry instructions to
    # remain inside this typed request rather than being prepended as free text.
    repair_attempt: Literal[2] | None = None
    repair_validation_error: str | None = Field(default=None, max_length=1000)


class Evidence(BaseModel):
    field: AllowedEvidenceField
    fragment: str


class ObservedMeasurement(BaseModel):
    # Positivity is enforced in a validator because the Gemini Developer API's
    # schema dialect does not accept JSON Schema's `exclusiveMinimum` keyword.
    value: Decimal
    uom: str = Field(min_length=1, max_length=20)
    field: AllowedEvidenceField
    fragment: str = Field(min_length=1)
    # Absent on readings stored before agent contract v3; treated as a product size.
    role: MeasurementRole | None = None

    @field_validator("uom")
    @classmethod
    def normalize_uom(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("value")
    @classmethod
    def require_positive_value(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("measurement value must be positive")
        return value

    @property
    def describes_product_size(self) -> bool:
        return self.role is None or self.role in PRODUCT_SIZE_ROLES


class PairInterpretation(BaseModel):
    """Meaning supported by one bilingual description pair.

    A silent language is not a conflict. CONFLICT is valid only when the two
    members of this same pair make incompatible claims about the same role.
    """

    pair: PairName
    status: PairStatus
    product_meaning: str | None = Field(default=None, max_length=240)
    evidence: list[Evidence] = Field(default_factory=list)
    conclusion: str = Field(min_length=1)

    @field_validator("conclusion", mode="before")
    @classmethod
    def clip_conclusion(cls, value: object) -> object:
        # Explanations are for people; a long one is trimmed, never a reason to
        # reject an otherwise valid reading and spend a second call.
        if isinstance(value, str) and len(value) > 400:
            return value[:397].rstrip() + "…"
        return value


class QuantityRelationship(BaseModel):
    relationship: RelationshipType
    subject: str = Field(min_length=1, max_length=120)
    amount: Decimal | None = None
    uom: str | None = Field(default=None, max_length=20)
    count: Decimal | None = None
    evidence: list[Evidence] = Field(default_factory=list)

    @field_validator("uom")
    @classmethod
    def normalize_optional_uom(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else None

    @model_validator(mode="after")
    def require_grounded_quantity(self) -> "QuantityRelationship":
        if self.amount is None and self.count is None:
            raise ValueError("relationship requires an amount or count")
        for value in (self.amount, self.count):
            if value is not None and (not value.is_finite() or value <= 0):
                raise ValueError("relationship quantities must be positive and finite")
        if self.amount is not None and not self.uom:
            raise ValueError("relationship amount requires a UOM")
        if not self.evidence:
            raise ValueError("relationship requires literal evidence")
        return self


class InferenceResult(BaseModel):
    status: Literal["PROPOSAL", "PACK_PROPOSAL", "NOT_IN_DESCRIPTION", "AMBIGUOUS", "CONFLICT"]
    measurement: ObservedMeasurement | None = None
    pack_size: Decimal | None = None
    pack_evidence: Evidence | None = None
    conflicting_measurements: list[ObservedMeasurement] = Field(default_factory=list)
    # Every other measurement the agent saw, with its role, so a choice can be audited
    # ("saw 180G and preferred 10G") and a non-size number is visible, not silently used.
    other_measurements: list[ObservedMeasurement] = Field(default_factory=list)
    pack_role: PackRole | None = None
    rationale: str | None = Field(default=None, max_length=400)
    # The model's own confidence is recorded but never drives a decision; the pipeline
    # derives confidence from guards and agreeing sources.
    confidence: Literal["HIGH", "MEDIUM", "LOW"] = "LOW"
    # Fully determined by status, so the model no longer has to supply it.
    reason_code: AgentReasonCode | None = None
    # v4 interpretation audit. Optional defaults preserve stored v3 responses.
    pair_interpretations: list[PairInterpretation] = Field(default_factory=list)
    quantity_relationships: list[QuantityRelationship] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_status_shape(self) -> "InferenceResult":
        if self.pack_size is not None and (
            self.pack_size <= 0 or self.pack_size != self.pack_size.to_integral_value()
        ):
            raise ValueError("pack size must be a positive whole number")
        if self.status == "PROPOSAL" and self.measurement is None:
            raise ValueError("PROPOSAL requires one observed measurement")
        if self.status == "PROPOSAL" and self.conflicting_measurements:
            raise ValueError("PROPOSAL cannot contain conflicting observations")
        if self.status == "PACK_PROPOSAL":
            if self.measurement is not None or self.conflicting_measurements:
                raise ValueError("PACK_PROPOSAL cannot contain measurement observations")
            if self.pack_size is None or self.pack_evidence is None:
                raise ValueError("PACK_PROPOSAL requires pack size and evidence")
        # "No size written" may still carry an explicit pack count ("SMALL CAN BEER 4'S").
        if self.status == "NOT_IN_DESCRIPTION" and (
            self.measurement is not None or self.conflicting_measurements
        ):
            raise ValueError("NOT_IN_DESCRIPTION cannot contain a measurement")
        if self.status == "AMBIGUOUS" and (
            self.measurement is not None
            or self.pack_size is not None
            or self.pack_evidence is not None
            or self.conflicting_measurements
        ):
            raise ValueError("AMBIGUOUS cannot contain a proposal or conflict set")
        if self.status == "CONFLICT":
            if self.measurement is not None or self.pack_size is not None or self.pack_evidence is not None:
                raise ValueError("CONFLICT cannot contain a selected proposal")
            if len(self.conflicting_measurements) < 2:
                raise ValueError("CONFLICT requires at least two observations")
        if (self.pack_size is None) != (self.pack_evidence is None):
            raise ValueError("pack size and pack evidence must be supplied together")
        expected_reason = _REASON_FOR_STATUS[self.status]
        # The reason code is fully determined by the status. A model that fills it
        # differently has not read anything wrong, so it is replaced, not rejected.
        self.reason_code = expected_reason  # type: ignore[assignment]
        return self


class ProviderMetadata(BaseModel):
    provider: Literal["mock", "google-adk"]
    agent_name: str
    agent_version: str
    prompt_version: str | None = None
    prompt_sha256: str | None = None
    model_id: str | None = None
    adk_version: str | None = None
    attempt_count: int = 1
    latency_ms: int = 0
    session_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    # Empty for a first-pass success. Populated after a malformed first answer
    # is repaired successfully by the single bounded schema-repair retry.
    validation_errors: list[str] = Field(default_factory=list)


class InferenceResponse(BaseModel):
    result: InferenceResult
    metadata: ProviderMetadata


class InferenceProvider(Protocol):
    async def infer(self, request: InferenceRequest) -> InferenceResponse: ...


def validate_evidence(request: InferenceRequest, result: InferenceResult) -> None:
    source = request.model_dump()
    evidence_items: list[Evidence | ObservedMeasurement] = []
    if result.measurement:
        evidence_items.append(result.measurement)
    if result.pack_evidence:
        evidence_items.append(result.pack_evidence)
    evidence_items.extend(result.conflicting_measurements)
    evidence_items.extend(result.other_measurements)
    for pair in result.pair_interpretations:
        evidence_items.extend(pair.evidence)
    for relationship in result.quantity_relationships:
        evidence_items.extend(relationship.evidence)
    for evidence in evidence_items:
        if evidence.field not in ALLOWED_EVIDENCE_FIELDS:
            raise ValueError(f"forbidden evidence field: {evidence.field}")
        if evidence.fragment not in (source.get(evidence.field) or ""):
            raise ValueError(f"evidence fragment is absent from {evidence.field}")
    if result.pack_evidence and result.pack_size is not None:
        if result.pack_evidence.field in {"item_brand_eng", "item_brand_local_lang"}:
            raise ValueError("pack evidence must come from a description field")
        count = str(result.pack_size.quantize(Decimal("1")))
        if not re.search(rf"(?<![\d.]){re.escape(count)}(?![\d.])", result.pack_evidence.fragment):
            raise ValueError("pack evidence does not contain the proposed count")
    if request.task == "PACK_ONLY":
        if result.status == "PROPOSAL" or result.measurement is not None or result.conflicting_measurements:
            raise ValueError("PACK_ONLY request cannot return measurement observations")
        if result.status not in {"PACK_PROPOSAL", "NOT_IN_DESCRIPTION", "AMBIGUOUS"}:
            raise ValueError("PACK_ONLY request returned an unsupported status")
    elif result.status == "PACK_PROPOSAL":
        raise ValueError("measurement request cannot return PACK_PROPOSAL")
