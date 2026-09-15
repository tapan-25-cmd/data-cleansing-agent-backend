from decimal import Decimal
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

AgentReasonCode: TypeAlias = Literal[
    "EXPLICIT_MEASUREMENT",
    "NOT_IN_DESCRIPTION",
    "AMBIGUOUS_DESCRIPTION",
    "DESCRIPTION_CONFLICT",
]


class InferenceProviderError(RuntimeError):
    pass


class InvalidInferenceResponseError(InferenceProviderError):
    pass


class InferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_brand_eng: str | None = None
    item_brand_local_lang: str | None = None
    item_desc_eng: str | None = None
    item_desc_local_lang: str | None = None
    web_description_eng: str | None = None
    web_description_chi: str | None = None


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


class InferenceResult(BaseModel):
    status: Literal["PROPOSAL", "NOT_IN_DESCRIPTION", "AMBIGUOUS", "CONFLICT"]
    measurement: ObservedMeasurement | None = None
    pack_size: Decimal | None = None
    pack_evidence: Evidence | None = None
    conflicting_measurements: list[ObservedMeasurement] = Field(default_factory=list)
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    reason_code: AgentReasonCode

    @model_validator(mode="after")
    def validate_status_shape(self) -> "InferenceResult":
        if self.pack_size is not None and self.pack_size <= 0:
            raise ValueError("pack size must be positive")
        if self.status == "PROPOSAL" and self.measurement is None:
            raise ValueError("PROPOSAL requires one observed measurement")
        if self.status == "PROPOSAL" and self.conflicting_measurements:
            raise ValueError("PROPOSAL cannot contain conflicting observations")
        if self.status == "NOT_IN_DESCRIPTION" and (
            self.measurement is not None
            or self.pack_size is not None
            or self.pack_evidence is not None
            or self.conflicting_measurements
        ):
            raise ValueError("NOT_IN_DESCRIPTION cannot contain observations")
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
    for evidence in evidence_items:
        if evidence.field not in ALLOWED_EVIDENCE_FIELDS:
            raise ValueError(f"forbidden evidence field: {evidence.field}")
        if evidence.fragment not in (source.get(evidence.field) or ""):
            raise ValueError(f"evidence fragment is absent from {evidence.field}")
