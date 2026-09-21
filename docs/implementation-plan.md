# UoM Data Cleansing Agent — Release 1 POC Implementation Plan

**Purpose:** Build a complete proof-of-concept implementation of the Release 1 requirements using **React + FastAPI + MongoDB**, with a chat-first Excel file-in/file-out workflow, deterministic unit conversion, a narrow AI/ADK inference path, read-only result inspection, and final Excel export.

**Primary source of truth:** `PRD_UoM_Data_Cleansing_Agent_R1_v1.1 (1).docx` (Draft v1.1, 11 Sep 2026) and `20260908_UoM_Snapshot_v0.2.xlsx`.

**Audience:** Codex / implementation agent, backend engineer, frontend engineer, QA.

**POC scope amendment (15 Sep 2026):** The unit-mapping administration/approval UI is
deferred. Deterministic rules are maintained as a validated, version-controlled ruleset
in the repository. Row-level A/B/C results remain visible as a read-only inspection panel.

**POC workflow amendment (16 Sep 2026):** The primary UI is an agent chat. The assistant
requests the workbook and exposes an upload action; the composer also always exposes a
`+` attachment action. Processing status, headline statistics, a compact preview, and
workbook download appear in the conversation. A/B/C results open in a right-side,
read-only panel. Review is optional and pending decisions never block export. This
amendment supersedes older approval-gate, approve/reject/override, and separate review-page
requirements elsewhere in this document.

> Important: This document is an implementation plan for the POC. Where the PRD contains an unresolved business decision, this plan does **not invent the answer**. It builds a configurable or safe fallback path and calls out the open decision explicitly.

---

## 0. Instructions to Codex

Build this as a working monorepo POC. Do not replace requirements with a generic CRUD app.

1. Use **React + TypeScript + Vite** for the frontend.
2. Use **Python 3.12+ + FastAPI + Pydantic v2** for the backend.
3. Use **MongoDB** for job state, row processing results, proposals, and review decisions. Deterministic unit rules live in a version-controlled ruleset in the codebase, not in MongoDB.
4. Use **openpyxl** for validated Excel input. For output, patch only the three standardized cells in the XLSX worksheet XML so the complete 23 MB workbook is not loaded and re-serialized.
5. Use **Google ADK + Gemini** behind an `InferenceProvider` interface. Provide a deterministic mock provider for tests and local development.
6. Use **local filesystem storage for POC files** behind a `FileStorage` abstraction. Do not use GridFS. A GCS adapter may be added later without changing business logic.
7. Never send Group A or Group B unit conversion to the LLM. A Group B pack-only call
   is allowed only after deterministic pack extraction finds an unresolved pack clue.
8. Keep deterministic unit rules in a declarative, versioned codebase ruleset with schema validation and automated tests. Do not build a mapping administration or approval UI for this POC.
9. Never let the LLM directly write Excel cells.
10. Never let the rule engine or AI layer depend on Excel column letters/positions. Business logic uses canonical field names only.
11. Never read `product_description` or `product_description_local` for AI inference. Preserve them in the workbook, but exclude them from processing DTOs.
12. Never modify `item_size_value` or `item_size_unit`.
13. Export applies available machine proposals by default. A/B/C inspection is optional and does not gate download.
14. Do not use live web search, external product APIs, images, or data-lake integration in R1.
15. Implement feature flags/configuration for unresolved/provisional requirements, especially pack-size inference.
16. All processing must be reproducible, testable, and traceable to PRD requirement IDs.

---

# 1. Business problem and POC goal

DFI product records should contain three standardized attributes:

- `Standardize Unit Size`
- `Standardize UOM`
- `Standardize Pack Size`

The standardized UOM must end as exactly one of:

- `GM` — weight
- `ML` — volume
- `EA` — count / each
- `FT` — length

Current data is manually entered and has two main defects:

1. values are missing;
2. values exist but use inconsistent units such as `G`, `GM`, `KG`, `OZ`, `LT`, `PC`, `PK`, etc.

The POC must:

- upload the provided Excel extract;
- validate its structure;
- process only the two R1 departments;
- skip and count purged rows;
- leave already-correct rows unchanged;
- deterministically convert rows that can be solved by a business-approved rule committed to the versioned ruleset;
- use AI only for rows that cannot be solved from structured legacy size/UOM data;
- detect description disagreements;
- show representative proposals in chat and all A/B/C results in a read-only side panel;
- allow export without completing review;
- generate a corrected Excel workbook while preserving the original workbook structure;
- measure accuracy using the PRD benchmark framework where the data supports it.

The POC does **not** write to any external DFI system.

---

# 2. Current workbook profile — verified against `v0.2`

The implementation must reproduce this profile before business logic is considered correct.

## 2.1 Workbook structure

- Workbook: `20260908_UoM_Snapshot_v0.2.xlsx`
- Size: approximately 23 MB
- Sheets: `Sheet2`, `UoM_Field_Extract`, `Sheet1`
- Processing input sheet: **`UoM_Field_Extract` only**
- `UoM_Field_Extract`: **66,082 data rows**, **30 columns**

## 2.2 Release 1 departments and counts

| Metric | Grocery 2 | Dairy & Frozen | Combined |
|---|---:|---:|---:|
| Raw department rows | 7,648 | 5,652 | 13,300 |
| Purged / skipped | 444 | 314 | 758 |
| Live / in scope | 7,204 | 5,338 | 12,542 |
| Group A — validated canonical K/L/M | 7,018 | 4,957 | 11,975 |
| Group B1 — non-base standardized value | 117 | 187 | 304 |
| Group B2 — standardized blank, legacy UOM exists | 17 | 153 | 170 |
| Group B3 — standardized-field canonicalization | 20 | 18 | 38 |
| Group B total | 154 | 358 | 512 |
| Group C — no legacy UOM, descriptions required | 32 | 23 | 55 |
| Unexpected live shapes | 0 | 0 | 0 |

Expected headline result:

```text
13,300 department rows
   -758 purged
=12,542 live rows

11,975 Group A
   512 Group B (304 B1 + 170 B2 + 38 B3)
    55 Group C
```

If the application does not reproduce these counts on the supplied baseline workbook, stop and treat it as a defect before continuing.

## 2.3 Actual Group B unit distribution

**Grocery 2 — B1:** `OZ`=40, `KG`=34, `LT`=18, `PC`=16, `PK`=2, `PACK`=2, `L`=2, `LB`=1, `FZ`=1, `Pack`=1

**Dairy & Frozen — B1:** `LT`=67, `PC`=46, `KG`=40, `OZ`=34

**B3 canonicalization:** Grocery 2 `G`=20; Dairy & Frozen `G`=18. These
rows have complete standardized fields, but `G` is normalized to canonical `GM` and the
existing standardized numeric values are preserved. Nearest-whole rounding applies only
to B1/B2 measurement conversions, not B3 representation cleanup.

**Grocery 2 — B2 legacy units:** `GM`=7, `PK`=4, `EA`=4, `PC`=1, `ML`=1

**Dairy & Frozen — B2 legacy units:** `PC`=132, `EA`=20, `ST`=1

This distribution is useful for rule-engine test coverage. Do not infer a rule merely because a unit appears here; rules must be explicitly reviewed and committed to the version-controlled ruleset.

---

# 3. Hard scope and non-goals

## In scope

- Departments:
  - `03_Grocery 2`
  - `06_Dairy & Frozen`
- Excel upload and download.
- Purge detection.
- Deterministic conversion.
- AI description inference for Scope 2b.
- Pack-size inference as a **feature-flagged provisional module**.
- Description discrepancy detection.
- On-screen review.
- Bulk review for deterministic conversion groups.
- Individual review for AI/exception items.
- Approve / reject / override.
- Two appended indicator columns.
- Output attention/review listing.
- Accuracy/evaluation tooling.

## Out of scope

- Remaining departments.
- Fresh departments.
- Live web search.
- Product photographs.
- Data lake integration.
- Direct write-back to item master or another DFI system.
- Multi-user concurrency guarantees.
- Full persisted audit history with agent version, approver timestamp, etc. (deferred by NFR-06).
- Production-grade queue infrastructure unless needed later.

---

# 4. Proposed POC architecture

```text
┌──────────────────────────────────────────────────────────────┐
│                         React UI                             │
│ Upload | Progress | Summary | B Review | C Review | Export │
└───────────────────────────────┬──────────────────────────────┘
                                │ HTTP/JSON
                                ▼
┌──────────────────────────────────────────────────────────────┐
│                        FastAPI API                           │
│ Jobs | Review | Export | Evaluation                        │
└─────────────┬───────────────────────┬────────────────────────┘
              │                       │
              ▼                       ▼
┌────────────────────────┐   ┌───────────────────────────────┐
│ Processing Application │   │       FileStorage            │
│                        │   │ LocalFileStorage for POC      │
│ Excel Adapter          │   │ later: GCSFileStorage        │
│ Purge Detector         │   └───────────────────────────────┘
│ Classifier             │
│ Rule Engine            │
│ AI Inference Service   │
│ Discrepancy Service    │
│ Review/Finalizer       │
│ Excel Exporter         │
└──────────────┬─────────┘
               │
       ┌───────┴────────┐
       ▼                ▼
┌──────────────┐  ┌───────────────────────────┐
│   MongoDB    │  │ Google ADK / Gemini      │
│ jobs        │  │ only Scope 2b / optional │
│ job_items   │  │ pack/ambiguous analysis  │
│ review data │  └───────────────────────────┘
└──────────────┘
```

## Architectural rule

Excel is an **adapter**, not the domain model.

The flow must be:

```text
Excel row
  -> canonical InputProduct
  -> ProductProcessingResult
  -> MongoDB proposal/review state
  -> FinalProductDecision
  -> Excel writer
```

Never:

```text
rule engine -> workbook cell directly
AI agent -> workbook cell directly
```

---

# 5. Suggested monorepo layout

```text
data-cleansing-agent/
├── README.md
├── docker-compose.yml
├── .env.example
├── docs/
│   └── implementation-plan.md
│
├── backend/
│   ├── pyproject.toml
│   ├── Dockerfile
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── dependencies.py
│   │   │
│   │   ├── api/
│   │   │   ├── jobs.py
│   │   │   ├── review.py
│   │   │   ├── export.py
│   │   │   └── evaluation.py
│   │   │
│   │   ├── domain/
│   │   │   ├── enums.py
│   │   │   ├── product.py
│   │   │   ├── proposal.py
│   │   │   ├── decision.py
│   │   │   └── unit_rule.py
│   │   │
│   │   ├── schemas/
│   │   │   ├── jobs.py
│   │   │   ├── review.py
│   │   │   └── inference.py
│   │   │
│   │   ├── services/
│   │   │   ├── job_service.py
│   │   │   ├── excel_reader.py
│   │   │   ├── excel_writer.py
│   │   │   ├── purge_detector.py
│   │   │   ├── classifier.py
│   │   │   ├── rule_engine.py
│   │   │   ├── inference_service.py
│   │   │   ├── pack_service.py
│   │   │   ├── discrepancy_service.py
│   │   │   ├── review_service.py
│   │   │   ├── export_service.py
│   │   │   └── evaluation_service.py
│   │   │
│   │   ├── agents/
│   │   │   ├── provider.py
│   │   │   ├── factory.py
│   │   │   ├── adk_provider.py
│   │   │   ├── mock_provider.py
│   │   │   ├── uom_inference_agent.py
│   │   │   └── prompts/
│   │   │       └── uom_inference_v1.md
│   │   │
│   │   ├── rules/
│   │   │   ├── README.md
│   │   │   ├── schema.py
│   │   │   ├── registry.py
│   │   │   └── unit_mappings.v1.yaml
│   │   │
│   │   ├── repositories/
│   │   │   ├── jobs.py
│   │   │   └── job_items.py
│   │   │
│   │   ├── storage/
│   │   │   ├── base.py
│   │   │   ├── local.py
│   │   │   └── gcs.py          # optional / stub for later
│   │   │
│   │   └── workers/
│   │       └── process_job.py
│   │
│   └── tests/
│       ├── unit/
│       ├── integration/
│       ├── fixtures/
│       └── acceptance/
│
└── frontend/
    ├── package.json
    ├── vite.config.ts
    ├── Dockerfile
    └── src/
        ├── app/
        ├── api/
        ├── pages/
        │   ├── UploadPage.tsx
        │   ├── ProcessingPage.tsx
        │   ├── SummaryPage.tsx
        │   ├── GroupBReviewPage.tsx
        │   ├── GroupCReviewPage.tsx
        │   ├── DiscrepancyPage.tsx
        │   └── ExportPage.tsx
        ├── components/
        ├── hooks/
        ├── types/
        └── styles/
```

---

# 6. Runtime dependencies

## Backend

Recommended:

```text
fastapi
uvicorn[standard]
pydantic>=2
pydantic-settings
pymongo
openpyxl
python-multipart
google-adk[gcp]>=2,<3
pytest
pytest-asyncio
httpx
```

Optional:

```text
orjson
structlog
```

## Frontend

Recommended:

```text
react
react-dom
typescript
vite
react-router-dom
@tanstack/react-query
```

Use the existing platform UI component library if it is available in the target repository. If it is not available, isolate POC components behind a small `components/ui/` layer so they can be replaced later.

---

# 7. Environment configuration

Provide `.env.example`:

```dotenv
APP_ENV=local
API_PREFIX=/api
MONGODB_URI=mongodb://mongo:27017
MONGODB_DB=uom_cleansing_poc

FILE_STORAGE_BACKEND=local
LOCAL_STORAGE_ROOT=/data/uom-jobs
MAX_UPLOAD_BYTES=104857600

AI_PROVIDER=mock
GEMINI_API_KEY=
GEMINI_MODEL=
AI_MAX_CONCURRENCY=5
AI_TIMEOUT_SECONDS=60

PACK_SIZE_INFERENCE_ENABLED=true
DISCREPANCY_AI_FALLBACK_ENABLED=false
REQUIRE_DISCREPANCY_ACK_BEFORE_EXPORT=true

BASE_UNITS=GM,ML,EA,FT

# Temporary POC output field names; O-9 remains open.
MACHINE_FILLED_COLUMN_NAME=Machine Filled Fields
DISCREPANCY_COLUMN_NAME=Discrepancy Flag

# Rounding policy is open O-7. Keep configuration explicit.
DEFAULT_ROUNDING_DECIMALS=
```

Do not silently set unresolved business rules in code.

---

# 8. Canonical field mapping

All business logic works with header names, not letters.

```python
FIELD_MAP = {
    "item_no": "Item_no",
    "division": "Division",
    "department": "Department",
    "category": "Category",
    "subcategory": "Subcategory",
    "section": "Section",

    "legacy_size": "item_size_value",
    "legacy_uom": "item_size_unit",

    "standard_size": "Standardize Unit Size",
    "standard_uom": "Standardize UOM",
    "standard_pack_size": "Standardize Pack Size",

    "web_description_eng": "web_description_eng",
    "web_description_chi": "web_description_chi",
    "item_brand_eng": "item_brand_eng",
    "item_brand_local": "item_brand_local_lang",
    "item_desc_eng": "item_desc_eng",
    "item_desc_local": "item_desc_local_lang",
}
```

`product_description` and `product_description_local` are preserved as workbook columns but must not be included in the inference DTO or any derived inference input.

---

# 9. Domain models

## 9.1 `InputProduct`

```python
class InputProduct(BaseModel):
    row_number: int
    item_no: str
    division: str | None
    department: str | None
    category: str | None
    subcategory: str | None
    section: str | None

    legacy_size: Decimal | str | None
    legacy_uom: str | None

    standard_size: Decimal | str | None
    standard_uom: str | None
    standard_pack_size: Decimal | str | None

    web_description_eng: str | None
    web_description_chi: str | None
    item_brand_eng: str | None
    item_brand_local: str | None
    item_desc_eng: str | None
    item_desc_local: str | None
```

Important:

- no B/C concatenated description fields;
- normalize empty strings to `None`;
- preserve item number as string, including leading zeros;
- do not coerce ambiguous numeric text into a number until validation.

## 9.2 Processing group

```python
class WorkGroup(str, Enum):
    A = "A"
    B = "B"
    C = "C"
    SKIPPED_PURGED = "SKIPPED_PURGED"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    DATA_SHAPE_ERROR = "DATA_SHAPE_ERROR"
```

## 9.3 Proposal method

```python
class ProposalMethod(str, Enum):
    NONE = "NONE"
    RULE = "RULE"
    AI_INFERENCE = "AI_INFERENCE"
    HUMAN_OVERRIDE = "HUMAN_OVERRIDE"
```

## 9.4 Reason codes

Minimum set:

```text
ALREADY_BASE_UNIT
RULE_CONVERSION
LEGACY_COPY_OR_CONVERSION
NO_RULE
NOT_IN_DESCRIPTION
DESCRIPTION_CONFLICT
AMBIGUOUS_DESCRIPTION
CATCH_WEIGHT_UNRESOLVED
PACK_SIZE_NOT_DERIVABLE
MALFORMED_VALUE
DATA_SHAPE_ERROR
```

Use reason codes in API and Mongo; user-facing text is derived in the UI.

---

# 10. Persistence and deterministic-rules design

## 10.1 `jobs`

```json
{
  "_id": "ObjectId",
  "job_id": "uuid",
  "original_file_name": "20260908_UoM_Snapshot_v0.2.xlsx",
  "input_storage_key": "jobs/<job_id>/input.xlsx",
  "output_storage_key": null,
  "status": "READY_FOR_REVIEW",
  "selected_departments": ["03_Grocery 2", "06_Dairy & Frozen"],
  "snapshot_label": "v0.2",
  "ruleset_version": "poc-v1",
  "stats": {
    "workbook_rows": 66082,
    "department_rows": 13300,
    "purged": 758,
    "live": 12542,
    "group_a": 11975,
    "group_b": 512,
    "group_c": 55,
    "discrepancies": 0
  },
  "progress": {
    "stage": "READY_FOR_REVIEW",
    "processed": 12542,
    "total": 12542,
    "percent": 100
  },
  "error": null,
  "created_at": "...",
  "updated_at": "..."
}
```

Indexes:

- unique `job_id`
- `created_at`
- `status`

## 10.2 `job_items`

Store one processing result for each in-scope or purged department row. 13,300 documents is small enough for the POC.

```json
{
  "job_id": "uuid",
  "row_number": 13776,
  "item_no": "037259",
  "department": "03_Grocery 2",
  "group": "SKIPPED_PURGED",

  "original": {
    "standard_size": null,
    "standard_uom": null,
    "standard_pack_size": null,
    "legacy_size": null,
    "legacy_uom": null
  },

  "field_proposals": {
    "standard_size": null,
    "standard_uom": null,
    "standard_pack_size": null
  },

  "method": "NONE",
  "reason_code": "...",

  "rule": {
    "rule_id": null,
    "factor": null,
    "source_uom": null,
    "target_uom": null
  },

  "evidence": [],
  "confidence": null,

  "discrepancy": {
    "flagged": false,
    "details": []
  },

  "review": {
    "field_decisions": {
      "standard_size": "PENDING",
      "standard_uom": "PENDING",
      "standard_pack_size": "PENDING"
    },
    "overall_status": "PENDING",
    "override_values": null,
    "comment": null
  }
}
```

Indexes:

- unique `(job_id, row_number)`
- `(job_id, item_no)`
- `(job_id, group)`
- `(job_id, review.overall_status)`
- `(job_id, rule.rule_id)`
- `(job_id, discrepancy.flagged)`

### Why per-field decisions matter

Pack-size inference applies to the same 225 blank rows as scopes 2a/2b. A B2 row may have deterministic size/UOM but an AI/provisional pack proposal. Bulk approval of a UOM rule must not automatically approve an unrelated pack-size proposal. Therefore decisions must be field-aware.

## 10.3 Deterministic unit rules are code-managed

Do not create a `unit_mappings` MongoDB collection. Store the deterministic rules in
`backend/app/rules/unit_mappings.v1.yaml` and commit every change through the normal
code-review process.

```yaml
version: poc-v1
rules:
  - rule_id: KG_TO_GM
    source_uoms: [KG]
    target_uom: GM
    operation: MULTIPLY
    factor: "1000"
    rounding_decimals: null
    enabled: true
    notes: Standard metric conversion
```

The packaged ruleset is immutable for the lifetime of a processing job. Persist its
version and SHA-256 checksum on the job so every proposal is reproducible. Changing a
rule requires a reviewed code change and a new ruleset version; there is no runtime
CRUD endpoint or mapping approval screen in this POC.

At application startup, validate that:

- the ruleset version is present;
- rule ids are unique;
- each normalized source UOM resolves to at most one enabled rule;
- factors are positive decimal strings;
- target UOM is one of `EA`, `GM`, `ML`, `FT`;
- only supported operations are used;
- unresolved units are absent rather than represented by guessed factors.

Keep the rules declarative. Do not scatter business mappings through `if/elif` code.

---

# 11. File storage strategy for the POC

## Decision

**Do not add dedicated object storage for the first POC.**

Use local file storage behind an interface:

```python
class FileStorage(Protocol):
    def save_input(self, job_id: str, file: BinaryIO) -> str: ...
    def get_input_path(self, job_id: str) -> Path: ...
    def save_output(self, job_id: str, path: Path) -> str: ...
    def get_output_path(self, job_id: str) -> Path: ...
```

POC implementation:

```text
/data/uom-jobs/<job_id>/input.xlsx
/data/uom-jobs/<job_id>/output.xlsx
```

Store only the storage keys/paths in MongoDB, not workbook bytes.

Do not use Mongo GridFS.

Later, add `GCSFileStorage` if deployed on ephemeral infrastructure or if files must survive restarts. Business services must not change when storage changes.

---

# 12. Job lifecycle

```text
UPLOADED
  -> VALIDATING
  -> PROFILING
  -> PROCESSING_RULES
  -> PROCESSING_DESCRIPTIONS
  -> CHECKING_DISCREPANCIES
  -> READY_FOR_REVIEW
  -> REVIEW_IN_PROGRESS
  -> READY_TO_EXPORT
  -> EXPORTING
  -> EXPORTED
```

Failure from any processing stage:

```text
FAILED
```

Rules:

- malformed workbook fails before partial processing output is produced;
- stage/progress is persisted in Mongo;
- user can refresh UI and continue viewing status;
- POC may use FastAPI `BackgroundTasks` / an in-process worker because R1 is single-user/single-file;
- worker abstraction should be easy to replace with Celery/RQ/Cloud Tasks later.

---

# 13. API flow — start to finish

## 13.1 Upload

`POST /api/jobs`

Multipart:

```text
file=<xlsx>
departments=03_Grocery 2,06_Dairy & Frozen
```

Response:

```json
{
  "job_id": "uuid",
  "status": "UPLOADED"
}
```

Validation before accepting:

- extension `.xlsx`;
- file opens as an Excel workbook;
- file size <= configured upload limit (100 MB default for POC; current source is ~23 MB);
- no path supplied by client is trusted;
- generate server-side job directory.

## 13.2 Start processing

`POST /api/jobs/{job_id}/process`

Response `202 Accepted`.

## 13.3 Poll status

`GET /api/jobs/{job_id}`

Return status, progress, counts, error.

Frontend polls every 1–2 seconds while processing.

## 13.4 Summary

`GET /api/jobs/{job_id}/summary`

Returns group counts, purge counts by department, discrepancy count, unresolved count, rule-group summary.

## 13.5 Review queries

```text
GET /api/jobs/{job_id}/items?group=B
GET /api/jobs/{job_id}/items?group=C
GET /api/jobs/{job_id}/items?discrepancy=true
GET /api/jobs/{job_id}/conversion-groups
```

Support pagination, default 50.

## 13.6 Bulk Group B decision

`POST /api/jobs/{job_id}/conversion-groups/{rule_id}/approve`

Payload may optionally specify which fields are being bulk-approved:

```json
{
  "fields": ["standard_size", "standard_uom"]
}
```

Exclude any row with:

- `NO_RULE`;
- discrepancy requiring individual review;
- invalid/ambiguous source;
- field already overridden individually.

Return counts approved/skipped.

## 13.7 Individual decision

`PATCH /api/jobs/{job_id}/items/{row_number}/decision`

Examples:

Approve:

```json
{
  "action": "APPROVE",
  "fields": ["standard_size", "standard_uom", "standard_pack_size"]
}
```

Reject:

```json
{
  "action": "REJECT",
  "fields": ["standard_size", "standard_uom"]
}
```

Override:

```json
{
  "action": "OVERRIDE",
  "values": {
    "standard_size": 500,
    "standard_uom": "GM",
    "standard_pack_size": 1
  },
  "comment": "Commercial verified against source"
}
```

Validate override UOM is one of `EA`, `GM`, `ML`, `FT`.

## 13.8 Export

`POST /api/jobs/{job_id}/export`

Final export should normally be blocked while required decisions remain pending.

`GET /api/jobs/{job_id}/download`

streams final XLSX.

---

# 14. Excel validation and reader

## 14.1 Required sheet

`UoM_Field_Extract`

Do not use `Sheet1` or `Sheet2` as input to the cleansing logic.

## 14.2 Required headers

At minimum validate all 30 expected headers from the baseline, because output preservation is a requirement. Processing-critical headers include:

```text
Item_no
Division
Department
Category
Subcategory
Section
item_size_value
item_size_unit
Standardize Unit Size
Standardize UOM
Standardize Pack Size
web_description_eng
web_description_chi
item_brand_eng
item_brand_local_lang
item_desc_eng
item_desc_local_lang
business_unit_no
business_unit_name
department_no
department_name
category_no
category_name
sub_category_no
sub_category_name
section_no
section_name
```

Also preserve the ignored columns exactly.

## 14.3 Header-based access only

Create a header-to-index map in the Excel adapter. Column positions may change without changing domain code.

Acceptance test:

- copy the workbook;
- move 3 columns to new positions;
- business logic result must be identical after only header mapping resolution.

---

# 15. Purge detection

A row is purged only when **all 14 fields from `item_brand_eng` through `section_name` are blank**:

```text
item_brand_eng
item_brand_local_lang
item_desc_eng
item_desc_local_lang
business_unit_no
business_unit_name
department_no
department_name
category_no
category_name
sub_category_no
sub_category_name
section_no
section_name
```

Algorithm:

```python
def is_purged(row: CanonicalRow) -> bool:
    return all(is_blank(row[field]) for field in PURGE_FIELDS)
```

Do **not** skip a row merely because one or several of these fields are blank.

Expected baseline:

```text
Grocery 2:       444
Dairy & Frozen:  314
Total:           758
```

Purged rows:

- remain in the original workbook;
- are not modified;
- are not sent to AI;
- are counted and shown to user;
- may have a `job_items` record with `group=SKIPPED_PURGED`.

---

# 16. Work classification algorithm

Run classification only after department scoping and purge detection.

Use exact blank normalization (`None`, empty string, whitespace-only string -> blank).

```python
def classify(row):
    if is_purged(row):
        return SKIPPED_PURGED

    if row.department not in configured_departments:
        return OUT_OF_SCOPE

    # Complete base-unit candidates pass the Group A validation gate first.
    if populated(row.standard_size)        and row.standard_uom in BASE_UNITS        and populated(row.standard_pack_size):
        return validate_group_a(row)

    # B1 / Scope 1: standardized size/UOM present but UOM is not base.
    if populated(row.standard_size)        and populated(row.standard_uom)        and row.standard_uom not in BASE_UNITS:
        return B

    # B2 / Scope 2a: standardized size/UOM blank, legacy UOM exists.
    if blank(row.standard_size)        and blank(row.standard_uom)        and populated(row.legacy_uom):
        return B

    # C / Scope 2b: standardized size/UOM blank and no legacy UOM.
    if blank(row.standard_size)        and blank(row.standard_uom)        and blank(row.legacy_uom):
        return C

    return DATA_SHAPE_ERROR
```

Baseline must produce zero `DATA_SHAPE_ERROR` rows for the two R1 departments.

Do not collapse discrepancy state into A/B/C. It is a separate flag.

---

# 17. Group A behavior

Expected on v0.2: **11,975** validated live rows.

Behavior:

- require positive finite numeric K;
- require exact canonical L (`EA`, `GM`, `ML`, `FT`);
- require positive whole-number M;
- reject formulas, Excel errors, invalid types, and duplicate item numbers;
- retain legacy and explicit-description comparisons as non-blocking warnings until
  their source-of-truth policy is confirmed;
- route known spelling/casing aliases to B3 deterministic canonicalization;
- route explicit bilingual measurement conflicts to validation review;
- no proposal for rows that pass;
- no automatic write;
- no normal review queue entry;
- preserve K/L/M as-is;
- may still be checked for description discrepancy;
- serves as the benchmark answer set.

Do not send Group A to Gemini merely to "verify" it during normal cleansing.

---

# 18. Group B deterministic rule engine

Expected: **512** rows total.

- Scope 1 / B1: 304
- Scope 2a / B2: 170
- Canonicalization / B3: 38

No language model is allowed in this path.

## 18.1 Rule lookup

Input:

```text
source value
source UOM
```

B1 source should normally come from the legacy I/J pair per FR-01 even though K/L currently contains a non-base representation. Preserve both for display and diagnostics.

B2 source comes from I/J.

Resolve the rule from the immutable registry loaded from the packaged ruleset:

```python
rule = rule_registry.get(source_uom)
```

If no rule:

```text
proposal = none
reason = NO_RULE
route = individual review
```

Do not ask AI to invent a conversion factor.

## 18.2 Calculation

For multiply mapping:

```text
raw_target = Decimal(source_value) * Decimal(factor)
final_target = excel_round_half_away_from_zero(raw_target, 0)
```

Use `Decimal`, not binary float, for conversion logic.

Every successful deterministic proposal stores:

- rule id;
- source UOM;
- target UOM;
- factor;
- source value;
- raw target before rounding;
- final proposed value after rounding.

## 18.3 Base-unit restriction

Rule output UOM must be one of:

```text
EA, GM, ML, FT
```

Reject a ruleset that targets any other unit during application startup and in tests.

## 18.4 Known decisions and unresolved mapping cases

Known from current PRD:

- container units such as `PK`, `PACK`, `Pack` are to become `EA` (O-4 closed);
- `FZ` factor is unresolved (US vs Imperial fluid ounce) — keep it out of the ruleset until the business decision is confirmed;
- catch weight `AV KG` is unresolved — do not force normal conversion;
- `SET` and `PR` collapsing to `EA` remains open;
- Group B1/B2 converted standardized size uses Excel-equivalent nearest-whole rounding;
- Group B3 canonicalization preserves existing numeric K/M values;
- source unit `ST` appears in current B2 data and should remain unmapped unless confirmed.

Safe POC behavior for unresolved units: `NO_RULE` and human review.

## 18.5 Initial version-controlled ruleset

A demo ruleset may contain standard mathematical mappings clearly marked as **POC sample / requires business approval before UAT**:

```json
[
  {"rule_id":"KG_TO_GM","source_uom":"KG","target_uom":"GM","factor":"1000","active":true},
  {"rule_id":"G_TO_GM","source_uom":"G","target_uom":"GM","factor":"1","active":true},
  {"rule_id":"LT_TO_ML","source_uom":"LT","target_uom":"ML","factor":"1000","active":true},
  {"rule_id":"L_TO_ML","source_uom":"L","target_uom":"ML","factor":"1000","active":true},
  {"rule_id":"PC_TO_EA","source_uom":"PC","target_uom":"EA","factor":"1","active":true},
  {"rule_id":"PK_TO_EA","source_uom":"PK","target_uom":"EA","factor":"1","active":true},
  {"rule_id":"PACK_TO_EA","source_uom":"PACK","target_uom":"EA","factor":"1","active":true}
]
```

Do not add `FZ` or `ST` without a business decision.

If `OZ` and `LB` are seeded for the demo, keep the exact mathematical factor separate from the still-open rounding policy.

---

# 19. Deterministic unit-rule maintenance

There is no unit-mapping administration or approval screen in this POC. There are no
runtime create/edit/activate mapping endpoints. Deterministic rules are maintained in
the version-controlled ruleset described in section 10.3.

Rule-change workflow:

1. Add or change a declarative ruleset entry.
2. Record the business decision or ticket in the rule notes and pull request.
3. Increment the ruleset version.
4. Run schema, duplicate-source, conversion, regression, and full-workbook profile tests.
5. Merge and deploy through the normal repository workflow.

The processing preflight and job summary should expose read-only rule readiness:

```text
Ruleset version: V
Ruleset checksum: SHA-256
Covered source UOMs: X
Uncovered source UOMs: Y
Affected rows: Z
```

This is operational information, not an administration screen. Allow processing with
uncovered units; they must safely route to review rather than fail the whole job.

---

# 20. Group C — AI inference

Expected current live workload: **55 rows**.

AI is used because there is no usable legacy UOM. Its task is narrow:

> Read only the permitted raw description/brand fields, identify explicit evidence for unit size/UOM and optionally pack size, surface conflicts, and decline when evidence is insufficient. Never use external knowledge to invent product size.

## 20.1 Allowed AI input fields

Only:

```text
item_brand_eng
item_brand_local_lang
item_desc_eng
item_desc_local_lang
web_description_eng
web_description_chi
```

Do not pass:

```text
product_description
product_description_local
Standardize Unit Size
Standardize UOM
Standardize Pack Size
```

For benchmark runs, ensure target fields are stripped at the DTO construction boundary.

## 20.2 `InferenceProvider` interface

```python
class InferenceProvider(Protocol):
    async def infer(self, request: InferenceRequest) -> InferenceResponse: ...
```

`InferenceResponse` wraps the validated `InferenceResult` together with provider,
agent, prompt, model, ADK, latency, attempt, and isolated-session provenance.

Implement:

- `AdkInferenceProvider`
- `MockInferenceProvider`

`AdkInferenceProvider` is backed by a real Google ADK `LlmAgent`, `App`, `Runner`, and
`InMemorySessionService`. `factory.py` selects the provider from `AI_PROVIDER`; choosing
`adk` with missing model/cloud configuration must fail startup rather than silently use
the mock. The rest of the app must not import Gemini/ADK directly.

The complete runtime, session-isolation, prompt-versioning, provenance, testing, and
rollout design is specified in [`docs/adk-agent-design.md`](adk-agent-design.md).

### 20.2.1 Real ADK agent boundary

Define `uom_description_inference_agent` in
`backend/app/agents/uom_inference_agent.py` using:

- a required, environment-pinned Gemini model;
- Pydantic `input_schema` and `output_schema`;
- a versioned prompt loaded from the repository;
- `temperature=0` and a bounded output-token limit;
- no tools, code execution, web access, artifacts, or long-term memory;
- a fresh ADK session for every product row;
- three total attempts for transient model/provider failures.

The agent extracts observations only. It must return `1 / KG` when the description says
`1 KG`; the backend `RuleEngine` performs the deterministic `KG -> GM` conversion. This
prevents conversion factors or arithmetic from drifting into the prompt/model layer.

## 20.3 Structured AI output

Use a strict Pydantic schema:

```python
class Evidence(BaseModel):
    field: Literal[
        "item_brand_eng",
        "item_brand_local_lang",
        "item_desc_eng",
        "item_desc_local_lang",
        "web_description_eng",
        "web_description_chi",
    ]
    fragment: str

class ObservedMeasurement(BaseModel):
    value: Decimal
    uom: str
    field: AllowedEvidenceField
    fragment: str

class InferenceResult(BaseModel):
    status: Literal[
        "PROPOSAL",
        "NOT_IN_DESCRIPTION",
        "AMBIGUOUS",
        "CONFLICT"
    ]
    measurement: ObservedMeasurement | None
    pack_size: Decimal | None
    pack_evidence: Evidence | None
    conflicting_measurements: list[ObservedMeasurement]
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    reason_code: str
```

Backend validation after AI response:

1. observed UOM must resolve through the deterministic `RuleRegistry`;
2. every evidence fragment must literally exist in its cited source field;
3. no evidence may cite a forbidden field;
4. `NOT_IN_DESCRIPTION` must not contain measurement/pack output;
5. conflict must include at least two observations and cannot be automatically resolved;
6. pack size must have separate literal evidence;
7. unsupported output is rejected and routed to manual review;
8. only the backend `RuleEngine` may create the final base-unit proposal.

## 20.4 Prompt rules

System prompt must explicitly say:

```text
- You are extracting evidence from supplied product text only.
- Do not use outside product knowledge.
- Report the measurement exactly as written; do not convert units or perform arithmetic.
- Do not guess a common package size.
- Do not assume a can is 330ML, a bottle is 500ML, etc.
- Do not infer from category alone.
- Consider all supplied English and local-language fields jointly.
- Cite exact field and exact text fragment for every proposal.
- If no explicit size/UOM appears, return NOT_IN_DESCRIPTION.
- If two sources state different sizes, return CONFLICT and both pieces of evidence.
- If a number appears but is likely a year/model/service code, do not treat it as size without unit evidence.
- Pack count and unit size are different concepts.
- Return only the structured schema.
```

## 20.5 Current-data reality

In the current 55 live Scope 2b rows:

- `web_description_eng` is empty for all 55;
- `web_description_chi` is empty for all 55;
- item descriptions are present;
- there is no obvious explicit `number + weight/volume UOM` signal in the permitted fields;
- some rows contain digits that are clearly not unit-size evidence (e.g. a year, charge code, version number);
- `172593` contains `4'S`, which is a pack-count clue but not a per-unit size/UOM.

Therefore a correct POC may produce many `NOT_IN_DESCRIPTION` results. That is expected and preferable to hallucination.

---

# 21. Pack-size module — implemented evidence-first pipeline

The capability is controlled independently through:

```text
PACK_SIZE_INFERENCE_ENABLED=true/false
```

Disabling the agent fallback does not affect deterministic UOM conversion or deterministic
pack extraction.

Processing order for Group B and C:

1. preserve a valid positive whole-number M;
2. normalize a valid numeric-text M without changing its value;
3. extract explicit pack patterns deterministically;
4. call the agent only when pack-like text is present but deterministic parsing cannot
   resolve it safely (Group C's normal measurement call may also return pack evidence);
5. accept an agent M only when the positive whole count occurs in an exact cited
   description fragment;
6. leave M blank on no evidence, conflict, invalid response, or provider error.

K/L and M retain independent field-level provenance. For a Group B row, K/L can be
`RULE` while M is `RULE`, `AI_INFERENCE`, `EXISTING`, or unresolved.

Rules:

1. If text explicitly represents a multipack such as `4 x 200ML`, proposal should be:

```text
unit size = 200
UOM = ML
pack size = 4
```

not `800 / ML / 1`.

2. Do not treat every piece count as pack size. Bare container/content counts require a
directly associated per-unit measurement or explicit pack wording.

Example acceptance fixture:

```text
IBN ABL BRAIS SAU(4PCS) 200GM
```

Do not propose `4`. Preserve an existing valid M (including `1`); otherwise route the
loose `4PCS` clue to the agent, which must decline or return ambiguity without stronger
pack evidence.

3. If pack size is silent, do not invent it.

4. O-8 remains open: blank pack size remains different from `1`; never silently fill `1`.

Recommended model:

```text
size/uom proposal decision != pack proposal decision
```

This permits deterministic size/UOM processing while pack resolution has independent
provenance and failure handling. Job statistics separately report existing, normalized,
deterministic, agent-proposed, declined, conflicting, missing, disabled, and error outcomes.

## 21.1 v0.2 workbook pack audit

The complete 66,082-row workbook was processed locally with the deterministic pipeline
and mock provider after implementation. The A/B/C invariants remained unchanged.

For the 512 Group B rows:

- 342 already contain a valid positive whole-number M and are preserved;
- 154 contain no explicit pack evidence and remain unchanged;
- 16 contain pack-like but unsafe/ambiguous clues and are selected for pack-only agent
  fallback;
- zero rows contain a deterministic explicit multipack pattern strong enough to fill M;
- zero deterministic conflicts or invalid existing M values were found.

All 55 Group C rows continue through the normal measurement agent call, which may also
return separately evidenced pack size. With the mock provider, the 16 B candidates and
55 C rows safely decline, producing 71 `pack_agent_declined` results and no M guesses.
Examples of the B fallback candidates include coupon/gift descriptions containing
`12PCS`, `4PC`, `1PC`, `GIFT PACK`, or `REFILL PACK`; these are intentionally not
accepted by the deterministic parser because the count may describe product contents
or voucher wording rather than a sellable pack.

---

# 22. Discrepancy detection

Discrepancy state is independent from A/B/C.

Compare size/pack information across exactly these pairs:

1. `item_brand_eng` vs `item_brand_local_lang`
2. `item_desc_eng` vs `item_desc_local_lang`
3. `web_description_eng` vs `web_description_chi`

## 22.1 Efficient POC implementation

Do **not** make 12,542 LLM calls merely to compare descriptions.

Implement a `DescriptionSignalExtractor` that deterministically extracts explicit numeric size/UOM and multipack patterns from text using configurable aliases and regex. Then compare normalized signals.

Optional AI fallback may be used only for ambiguous cases if `DISCREPANCY_AI_FALLBACK_ENABLED=true`.

## 22.2 Flagging rules

Flag when both sides of a pair contain explicit size/pack signals and the normalized signals conflict.

Example:

```text
English: 500ML
Chinese: 1L
```

Result:

```json
{
  "flagged": true,
  "pair": "item_desc",
  "left": {"field":"item_desc_eng","value":"500 ML"},
  "right": {"field":"item_desc_local_lang","value":"1000 ML"}
}
```

Do not automatically pick a winner.

A row may be:

```text
Group A + discrepancy=true
Group B + discrepancy=true
Group C + discrepancy=true
```

For Group B rows with discrepancy, remove them from bulk approval and require individual review.

---

# 23. Review model and human-in-the-loop behavior

Core principle:

```text
Machine proposes.
Commercial decides.
```

No proposal becomes a final workbook value until approved or overridden.

## 23.1 Group A

- not shown in normal review queue;
- if discrepancy flagged, show in discrepancy queue for acknowledgement/investigation;
- no machine change to approve.

## 23.2 Group B

Group by deterministic rule/conversion type:

```text
KG -> GM
OZ -> GM
LT -> ML
PC -> EA
PK -> EA
...
```

Show:

- item number;
- description/context;
- current K/L/M;
- source I/J;
- proposed K/L/M;
- rule id;
- factor;
- rounding used;
- count in group.

Allow one action per conversion group for size/UOM proposals.

Target: clear all 512 deterministic rows in fewer than 15 actions, except rows intentionally removed from bulk flow due to discrepancy/no-rule/individual override.

## 23.3 Group C

One item at a time.

Display:

- item no;
- hierarchy;
- all six permitted raw text fields;
- current K/L/M;
- AI proposed K/L/M, if any;
- evidence field(s);
- highlighted evidence fragment(s);
- confidence;
- reason code and explanation;
- discrepancies;
- actions.

Actions:

```text
Approve
Reject / leave original
Override
```

For `NOT_IN_DESCRIPTION`, UI should present:

```text
Agent could not derive a size from supplied descriptions.
[Accept no-change] [Override manually]
```

## 23.4 Overrides

Override validation:

- UOM must be EA/GM/ML/FT;
- numeric fields must be positive unless business explicitly allows zero;
- pack size cannot be negative;
- store reviewer comment in POC when override is used.

---

# 24. Frontend pages and UX

## 24.1 Upload page

Show:

- file selector;
- selected departments (default both R1 departments, locked or configurable);
- read-only ruleset version and source-UOM coverage summary;
- Process button.

After upload, show filename and file size.

## 24.2 Processing page

Stages:

```text
Validating workbook
Profiling departments
Detecting purged products
Applying unit rules
Running description inference
Checking discrepancies
Preparing review
```

Show progress and counts as available.

## 24.3 Summary page

Cards:

```text
Raw in departments       13,300
Purged                       758
Live                       12,542
Already correct            11,975
Rule fixes                    512
Needs interpretation           55
Discrepancies                   X
Unmapped/no-rule                X
Pending review                  X
```

Per-department table below.

## 24.4 Group B review page

Left side / table:

```text
Rule / conversion | rows | approved | pending | exceptions
```

Click group -> detail list.

Bulk approve button.

## 24.5 Group C review page

Card/detail experience with previous/next navigation and keyboard-friendly review.

Filters:

```text
All
Proposal
Not in description
Ambiguous
Conflict
No rule
```

## 24.6 Discrepancy page

Display pair, both signals, and linked item review.

## 24.7 Export page

Show readiness checklist:

```text
[x] Group B rule proposals reviewed
[x] Group C items reviewed
[x] Required discrepancy acknowledgements complete
[x] No invalid output UOM
[x] Ruleset version/checksum recorded
```

Then generate/download.

---

# 25. Processing worker — exact sequence

Implement processing in this order:

```text
1. Load input workbook metadata.
2. Validate UoM_Field_Extract exists.
3. Validate headers.
4. Read rows from UoM_Field_Extract.
5. Filter Department to R1 departments for processing.
6. For each R1 row, detect purge.
7. Persist purged row results and counts.
8. Convert non-purged rows to canonical InputProduct objects.
9. Classify A/B/C.
10. Persist classification results.
11. Process all Group B rows through RuleEngine.
12. Persist rule proposals or NO_RULE exceptions.
13. Process Group C rows through InferenceProvider with bounded concurrency.
14. If pack module enabled, process pack candidates/blank pack fields.
15. Run discrepancy detection independently across live rows.
16. Mark exception rows as requiring individual review.
17. Compute summary counts.
18. Verify profile invariants.
19. Set job READY_FOR_REVIEW.
```

## Bounded AI concurrency

Current C workload is only 55. Use configurable bounded concurrency, e.g. 5.

```text
AI_MAX_CONCURRENCY=5
```

Do not submit 55 calls unbounded.

Retry policy:

- retry transient provider/network failures 2 times with exponential backoff;
- do not retry schema-invalid output endlessly;
- after retries, route item to review with `AI_PROVIDER_ERROR` rather than fail the entire workbook.

---

# 26. Excel export design

Export starts from an untouched copy of the original input workbook.

## 26.1 Main sheet

`UoM_Field_Extract`:

- preserve every original row;
- preserve every original column and original order;
- modify only approved/overridden values in:
  - `Standardize Unit Size`
  - `Standardize UOM`
  - `Standardize Pack Size`
- append exactly two configured indicator columns at the end:
  - machine-filled indicator;
  - discrepancy flag.

Never modify:

- `item_size_value`
- `item_size_unit`
- ignored columns;
- purged rows;
- out-of-scope department rows.

## 26.2 Machine-filled indicator

O-9 leaves final vocabulary open. For the POC use configurable values, for example:

```text
SIZE
UOM
PACK
SIZE|UOM
SIZE|UOM|PACK
NONE
```

Only mark a field if the final exported value differs due to an approved machine proposal or human override of a machine proposal.

## 26.3 Discrepancy indicator

Example:

```text
NONE
ITEM_DESC_CONFLICT
WEB_DESC_CONFLICT
BRAND_CONFLICT
MULTIPLE
```

Final vocabulary remains configurable.

## 26.4 Additional output sheets

The PRD requires a full extract plus a separate listing of items needing attention, and requires current values beside proposed values. To satisfy this without adding many columns to the main extract, create:

### `Cleansing_Run_Summary`

Include:

- job id;
- input filename;
- ruleset version and checksum;
- departments;
- raw counts;
- purged count by department;
- A/B/C counts;
- discrepancy count;
- approval counts;
- export timestamp (POC metadata; full audit remains deferred).

### `Cleansing_Attention`

Include at least:

```text
Item_no
Department
Group
Reason
Current Unit Size
Current UOM
Current Pack Size
Proposed Unit Size
Proposed UOM
Proposed Pack Size
Final Unit Size
Final UOM
Final Pack Size
Method
Rule ID
Factor
Evidence Field
Evidence Fragment
Confidence
Discrepancy
Decision
Reviewer Comment
```

Include all C items, no-rule items, discrepancy items, rejected items, and overridden items. Optionally include all changed rows for stronger POC provenance.

This sheet resolves the current/proposed visibility requirement without violating the two-indicator-column design on the main extract.

## 26.5 Export validation

After saving output:

1. reopen with openpyxl;
2. confirm all original sheet names still exist;
3. confirm all original columns remain in original order;
4. confirm I/J are byte/value-equivalent row-by-row;
5. confirm changes are restricted to K/L/M + appended columns + added POC summary/attention sheets;
6. confirm all final Standardize UOM values written by the POC are EA/GM/ML/FT;
7. confirm purged and out-of-scope source rows unchanged;
8. confirm workbook opens successfully.

---

# 27. Idempotency

PRD FR-36 requires re-running over its own output to produce no further changes.

Acceptance test:

```text
input.xlsx
  -> process + approve + export
  -> output1.xlsx
  -> process again
  -> output2 proposals
```

Expected:

```text
No additional K/L/M changes proposed for values already normalized and approved.
```

The appended indicator columns must be ignored by classification logic.

---

# 28. Accuracy / evaluation framework

Build evaluation as a separate service/endpoints, not mixed into the normal cleansing job.

## 28.1 Deterministic conversion KPI K-1

Target: >= 99%.

Method:

1. select benchmark rows from completed products;
2. keep expected K/L/M separately;
3. hide K/L/M from processing input;
4. run deterministic conversion from legacy I/J;
5. compare predicted size/UOM to expected output under confirmed mapping;
6. report department and scope breakdown.

Rule engine itself should be mathematically deterministic; mismatches against human baseline must be listed for adjudication rather than silently "fixed" to match human data.

## 28.2 AI recommendation KPI K-2

Target: >= 90%.

**Current requirement/data gap:** the PRD calls for a benchmark subset where the answer could only come from the product name, but current benchmark rows retain legacy I/J values, so ordinary routing sends them through Scope 2a rather than AI. The current snapshot does not by itself define a valid AI benchmark subset under the normal routing rules.

Implement the evaluation framework but require a curated AI benchmark fixture/list approved by Product/QA. Do not fabricate the K-2 score from the 55 live blanks because they have no known answer key.

Recommended benchmark record:

```json
{
  "item_no": "...",
  "input_fields": {...},
  "expected": {"unit_size": 335, "uom": "GM", "pack_size": 1},
  "source": "commercial-approved-test-set"
}
```

For an AI-only benchmark run, ensure I/J and K/L/M are hidden from the inference pipeline.

## 28.3 Exception classification KPI K-3

Target: >= 95%.

Track:

```text
Correctly declined
Wrongly declined
```

Needs a labeled/curated test set where reviewers know whether a rule or description could have settled the item.

## 28.4 Reporting

Always report:

- Grocery 2 separately;
- Dairy & Frozen separately;
- combined;
- Scope 1 separately;
- Scope 2a separately;
- Scope 2b separately.

Do not hide AI performance behind easy Group A rows.

---

# 29. Error handling

## File-level fatal errors

Fail job and produce no partial output for:

- not an XLSX;
- unreadable/corrupt workbook;
- missing `UoM_Field_Extract`;
- missing required headers;
- duplicate required headers;
- invalid workbook structure preventing safe row identity.

## Row-level safe exceptions

Do not fail whole job for:

- unmapped source UOM;
- malformed single row value;
- AI decline;
- AI provider failure after retry;
- description conflict.

Route these to attention/review with explicit reason.

## Critical safety errors

Treat as fatal / test failure:

- any change to I/J;
- loss/reorder of original columns;
- benchmark leakage;
- write to external system;
- silent application of unapproved proposal.

---

# 30. Logging and observability

POC structured logs should include:

```text
job_id
stage
row_number when applicable
item_no when applicable
rule_id when applicable
reason_code
provider request id if available
elapsed_ms
```

Do not log full workbook rows by default.

Persist processing stage/progress in Mongo so UI state survives refresh.

---

# 31. Testing plan

## 31.1 Unit tests

### Excel adapter

- maps by header name, not column index;
- preserves leading-zero item numbers;
- normalizes blanks;
- excludes B/C from inference DTO.

### Purge detector

- all 14 fields blank -> purged;
- 13 blank + 1 populated -> live;
- current workbook gives exactly 758.

### Classifier

- Group A complete base unit;
- B1 non-base standardized unit;
- B2 blank standardized + legacy unit;
- C blank standardized + no legacy unit;
- unexpected state -> DATA_SHAPE_ERROR;
- current workbook exact counts.

### Rule engine

- ruleset schema and startup validation;
- rule-registry lookup;
- unique rule ids and no duplicate enabled source UOM;
- only EA/GM/ML/FT targets;
- factors represented and calculated as `Decimal`;
- ruleset version and checksum captured on the job;
- Decimal multiplication;
- no rule -> NO_RULE;
- output target restricted to base units;
- rule/factor captured;
- repeated conversion produces identical result;
- Excel-equivalent nearest-whole B1/B2 conversion tests, including half values;
- B3 canonicalization tests proving existing numeric K/M values are not rounded.

### AI result validation

- exact evidence fragment exists;
- bad source field rejected;
- forbidden B/C evidence rejected;
- NOT_IN_DESCRIPTION cannot contain proposal;
- unsupported UOM rejected;
- conflict cannot be auto-applied.
- observed measurements are normalized only by the deterministic RuleEngine;
- separate ADK session id is used for every product;
- malformed/missing final ADK event becomes a row-level provider error.

### Pack rules

- `4 x 200ML` -> 200 / ML / 4;
- `IBN ABL BRAIS SAU(4PCS) 200GM` -> pack 1 per PRD fixture;
- silent -> no invented pack.

### Discrepancy

- 500ML vs 500ML -> no flag;
- 500ML vs 1L -> flag after normalization;
- only one side contains a size -> no conflict by default;
- discrepancy does not alter A/B/C group.

### Review

- no approval -> no final value;
- approve -> proposal becomes final;
- reject -> original retained;
- override -> validated override final;
- bulk approve does not approve pack field accidentally.

## 31.2 Integration tests against supplied workbook

Must assert:

```text
66082 total rows
7648 Grocery 2
5652 Dairy & Frozen
444 + 314 purged
7204 + 5338 live
7018 + 4957 Group A
117 + 187 B1
17 + 153 B2
20 + 18 B3
32 + 23 Group C
0 unexpected live shapes
```

Also assert exact B1/B2 source-unit distributions listed in section 2.3.

## 31.3 Export tests

- output opens;
- original columns/order preserved;
- I/J unchanged across full workbook;
- only allowed cells changed;
- rejected-all export leaves source data values unchanged;
- approved deterministic proposal writes expected K/L;
- appended indicators exist;
- attention and summary sheets exist;
- second run is idempotent.

## 31.4 Frontend tests

At minimum:

- upload valid/invalid file;
- progress polling;
- summary rendering;
- bulk Group B approve;
- individual C approve/reject/override;
- unresolved export gate;
- final download.

---

# 32. POC feature flags / unresolved business issues

Do not block coding the architecture on these, but do not invent final behavior either.

| Issue | POC behavior |
|---|---|
| O-1 Catch weight `AV KG` | Route to review / `CATCH_WEIGHT_UNRESOLVED` until decision. |
| O-2 `FZ` conversion | Keep unmapped. Item `185611` should become `NO_RULE` until factor chosen. |
| O-3 Pack-size reliability | Evidence-first B/C pipeline implemented behind `PACK_SIZE_INFERENCE_ENABLED`; deterministic extraction remains active when agent fallback is disabled. |
| O-6 SET / PR -> EA | No mapping until confirmed. |
| O-7 rounding | Closed for POC: B1/B2 conversions use Excel-equivalent nearest whole; Group A and B3 existing numeric values are never rounded. |
| O-8 blank pack vs 1 | Do not blanket-fill 1 until decided. |
| O-9 indicator names/vocabulary | Configurable names and value vocabulary. |
| O-10 I/J vs descriptions disagree | Flag for review; do not silently establish precedence. |
| O-11 snapshot version control | Store input checksum/name/profile; new snapshot triggers re-profile. |
| O-12 language header naming | Use current exact headers via field map; domain names stay canonical. |

---

# 33. Known PRD-vs-data gaps to raise with Product/Commercial

## 33.1 FR-07 Betty Crocker acceptance fixture is invalid on v0.2

Item `037259` contains `335GM` in concatenated `product_description`, but the raw allowed inference fields are blank. It also satisfies the purge rule. Since B/C are forbidden inference inputs, this item cannot validly demonstrate FR-07 on the current baseline.

Action:

- replace the FR-07 fixture with a live, non-purged row whose allowed raw description field contains explicit size evidence; or
- supply a dedicated synthetic/curated UAT fixture.

Do not weaken FR-08 to make this example pass.

## 33.2 K-2 AI benchmark is underspecified for current snapshot

The benchmark rows have legacy I/J available. If K/L/M alone are blanked, normal routing becomes deterministic Scope 2a and AI is bypassed.

Action:

- Product/QA supplies a curated AI benchmark subset and expected answers;
- evaluation mode can additionally mask I/J for that curated subset.

Do not report a fabricated 90% AI score from unlabeled live blanks.

## 33.3 FR-24 current/pre-change values in output

The main extract allows only K/L/M plus two appended indicator columns, but FR-24 requires current beside proposed in the output file.

POC resolution:

- use `Cleansing_Attention` sheet for current/proposed/final values.

Confirm with Product that this layout is acceptable before final UAT.

---

# 34. Special regression fixtures from the current workbook

## Item `037259`

Expected POC behavior:

```text
purged -> skipped
must not read concatenated B/C to recover 335GM
```

This is a strong regression test for purge + forbidden-field enforcement.

## Item `185611`

Current data contains `16 FZ`.

Expected until O-2 resolved:

```text
Group B candidate
mapping lookup for FZ fails
reason = NO_RULE
individual review
no AI-derived conversion factor
```

## Item `172593`

Allowed item description contains `SMALL CAN BEER 4'S` / local equivalent.

Expected safe behavior:

```text
possible pack-count evidence = 4
unit size/UOM not explicitly present
must not infer e.g. 330ML from product knowledge
```

Likely AI result:

```text
NOT_IN_DESCRIPTION for unit size/UOM
pack evidence may be surfaced separately if pack feature enabled
```

---

# 35. Security / POC safeguards

- Accept `.xlsx` only.
- Enforce upload size limit.
- Server generates storage paths; never use raw client filename as path.
- Reject malformed workbooks before partial output.
- No external URLs in inference input.
- No live web search tools attached to the agent.
- No outbound writes to business systems.
- Validate all human override values server-side.
- Escape/sanitize any text written to new Excel cells if it could be interpreted as an Excel formula (`=`, `+`, `-`, `@`) when the value is intended as literal text.
- CORS limited to configured frontend origin.
- POC can run without full authentication in a trusted internal environment, but isolate auth behind middleware so it can be added later.

---

# 36. Docker Compose for local POC

Provide services:

```text
frontend
backend
mongo
```

Mount backend job storage as a local volume so browser refresh/server container restart does not immediately lose files during development:

```yaml
volumes:
  mongo_data:
  uom_job_files:
```

Backend mounts:

```text
/data/uom-jobs
```

Do not add Redis/Celery for the first POC unless actual runtime behavior requires it.

---

# 37. Build sequence for Codex

Implement in this order so each phase is independently testable.

## Phase 1 — repository + runtime

- monorepo folders;
- React/Vite/TS;
- FastAPI;
- Mongo connection;
- Docker Compose;
- health endpoints;
- `.env.example`.

Exit criteria:

```text
frontend loads
backend /health returns OK
Mongo reachable
```

## Phase 2 — file upload/storage

- `FileStorage` interface;
- local implementation;
- upload endpoint;
- job record creation;
- workbook validation.

Exit criteria:

```text
current 23MB workbook uploads successfully
bad XLSX rejected clearly
```

## Phase 3 — Excel adapter + profiling

- header map;
- canonical row mapping;
- department filtering;
- purge detector;
- summary profiler.

Exit criteria: exact baseline counts from section 2.

## Phase 4 — A/B/C classification

- classifier;
- Mongo `job_items` bulk persistence;
- unit tests;
- summary API.

Exit criteria: exact A/B1/B2/C counts.

## Phase 5 — version-controlled ruleset + rule engine

- declarative `unit_mappings.v1.yaml` ruleset;
- schema validation and immutable in-process registry;
- ruleset version/checksum job provenance;
- documented code-review workflow for rule changes;
- Decimal conversion;
- rule provenance;
- NO_RULE path.

Exit criteria:

- no LLM in rule path;
- repeated run stable;
- FZ safely unresolved unless configured.

## Phase 6 — Group B review UI

- conversion-group API;
- bulk approval;
- individual exception override;
- current/proposed comparison.

Exit criteria: 512 deterministic items navigable/groupable.

## Phase 7 — ADK inference

**Implementation status (15 Sep 2026):** implemented and covered by offline tests.
The credentialed Gemini Developer API smoke test and curated live evaluation remain
explicit gates because they make real model calls and incur API usage.

- provider interface;
- provider factory with fail-fast `mock` / `adk` selection;
- mock provider;
- real `uom_description_inference_agent` using ADK `LlmAgent` + `App`;
- ADK provider using `Runner` and one isolated session per row;
- versioned prompt with version/checksum provenance;
- strict Pydantic input and observed-measurement output schemas;
- literal evidence and post-ADK response validation;
- deterministic normalization of the observed measurement through `RuleEngine`;
- bounded concurrency;
- transient retry, timeout, and row-level provider-error handling;
- recorded/fake-event integration tests plus opt-in live evaluation;
- Group C persistence.

Exit criteria: 55 rows processed without hallucinated default sizes; Group A/B make zero
ADK calls; provider failures become review exceptions; every accepted proposal records
agent, prompt, model, ADK version, evidence, and validation provenance.

## Phase 8 — Group C review UI

- individual cards;
- evidence display;
- confidence;
- approve/reject/override;
- filters.

## Phase 9 — pack-size evidence pipeline — complete

- deterministic patterns and safe ambiguity handling;
- selective pack-only ADK fallback;
- strict whole-number and literal-evidence validation;
- field-level provenance and pack outcome statistics;
- PRD fixture coverage and export preservation.

## Phase 10 — discrepancy detection

- deterministic signal extractor;
- pair comparison;
- flag UI;
- exclude flagged B rows from blind bulk approval.

## Phase 11 — export

- copy original workbook;
- apply final decisions only;
- append indicators;
- create run summary;
- create attention sheet;
- validation/reopen;
- download endpoint.

Exit criteria: I/J unchanged and original columns/order preserved.

## Phase 12 — evaluation tooling

- deterministic benchmark;
- curated AI benchmark format;
- exception classification metrics;
- reports by department/scope.

## Phase 13 — acceptance test suite

- all PRD must-have requirements;
- actual workbook profile regression;
- special items;
- idempotency;
- rejected-all path;
- output preservation.

---

# 38. Requirement traceability — Functional Requirements

| Requirement | POC implementation |
|---|---|
| FR-01 | `RuleEngine` derives base size/UOM from I/J via the version-controlled ruleset. |
| FR-02 | Rule path contains no LLM; reproducibility unit/integration test. |
| FR-03 | Store `rule_id` + `factor` on every deterministic proposal. |
| FR-04 | Missing mapping -> `NO_RULE`, no output value, individual review. |
| FR-05 | Excel writer guard + full-workbook diff test ensures I/J unchanged. |
| FR-06 | Group A unchanged. |
| FR-07 | `InferenceProvider` reads permitted raw fields and proposes size/UOM. |
| FR-08 | Inference DTO omits B/C; test blocks any reference. |
| FR-09 | All description fields passed jointly, no hardcoded field precedence. |
| FR-10 | Evidence field + exact fragment required and backend-validated. |
| FR-11 | Confidence enum visible in Group C UI. |
| FR-12 | No size evidence -> `NOT_IN_DESCRIPTION`, no guess. |
| FR-13 | English + local fields in request and test fixtures. |
| FR-14 | Feature-flagged pack derivation for 225 blank-pack rows. |
| FR-15 | Multipack decomposition test `4 x 200ML`. |
| FR-16 | Piece-count-inside-pack negative fixture. |
| FR-17 | Silent pack -> decline/no invented value. |
| FR-18 | `DiscrepancyService` compares the three defined pairs. |
| FR-19 | Store pair and both normalized signals/evidence. |
| FR-20 | Conflict never automatically resolves/writes. |
| FR-21 | Discrepancy flag independent of A/B/C. |
| FR-22 | Machine-filled indicator appended; field-level. |
| FR-23 | Separate discrepancy indicator appended. |
| FR-24 | UI shows current/proposed; `Cleansing_Attention` sheet retains them. |
| FR-25 | Group A excluded from normal review queue. |
| FR-26 | Group B grouped by conversion/rule with bulk action. |
| FR-27 | Group C individual view with reason/evidence/confidence. |
| FR-28 | Approve/reject/override APIs and UI. |
| FR-29 | Export applies only approved/overridden proposals. |
| FR-30 | Reader uses only `UoM_Field_Extract` for processing. |
| FR-31 | Purge rule exact all-14-fields test. |
| FR-32 | Skip count shown by department + summary sheet. |
| FR-33 | XLSX in/out, original columns/order preserved. |
| FR-34 | Diff validation restricts source-sheet changes. |
| FR-35 | Full extract + separate `Cleansing_Attention` listing. |
| FR-36 | Idempotency second-pass test. |
| FR-37 | No external write client in deployed POC. |
| FR-38 | Evaluation service implements blind benchmark framework. |
| FR-39 | Benchmark DTO strips answer fields; test for leakage. |
| FR-40 | Report per department + combined. |
| FR-41 | Report Scope 1 / 2a / 2b separately. |
| FR-42 | Disagreement report for human vs agent adjudication. |
| FR-43 | 225 live blank rows excluded from normal baseline accuracy score. |

---

# 39. Requirement traceability — Non-functional Requirements

| Requirement | POC implementation |
|---|---|
| NFR-01 | KPI/evaluation suite, subject to valid labeled test sets. |
| NFR-02 | Background processing + persisted progress + processing screen. |
| NFR-03 | Department parameter/config, no hardcoded business branch. |
| NFR-04 | Header mapping, I/O adapters, structured result, no cell writes in business logic. |
| NFR-05 | POC scope decision: deterministic rules use a declarative, versioned codebase ruleset with review history; runtime Mongo editing/admin UI is deferred. |
| NFR-06 | Full independent audit store intentionally not built; file-level provenance retained. |
| NFR-07 | UI component layer; use existing platform library if available. |
| NFR-08 | English/local fields processed; bilingual fixtures required. |
| NFR-09 | Structural validation before processing, no partial output on malformed workbook. |

---

# 40. Definition of Done for the POC

The POC is complete when all of the following are true:

- [ ] React frontend and FastAPI backend run via Docker Compose.
- [ ] MongoDB persists job, item, proposal, and review state; jobs record the ruleset version/checksum.
- [ ] Local file-storage adapter saves input/output XLSX.
- [ ] Current v0.2 workbook uploads successfully.
- [ ] Only `UoM_Field_Extract` is processed.
- [ ] Current profile exactly reproduces 13,300 / 758 / 12,542 / 11,975 / 512 / 55.
- [ ] Purged rows are skipped and reported.
- [ ] Group A passes deterministic K/L/M validation and is otherwise untouched.
- [ ] Group B uses the validated, version-controlled deterministic ruleset with no LLM.
- [ ] Unmapped units become `NO_RULE`, never guessed.
- [ ] Group C uses ADK only on permitted raw fields.
- [ ] Real ADK `LlmAgent` is selected through configuration and uses an isolated session per Group C row.
- [ ] ADK extracts observed measurements; deterministic code performs every unit conversion.
- [ ] Every AI result records agent, prompt, model, and validation provenance.
- [ ] AI proposals always show evidence + confidence.
- [ ] AI can safely decline with `NOT_IN_DESCRIPTION`.
- [ ] Pack-size module is feature-flagged.
- [ ] Discrepancies are independently flagged and never auto-resolved.
- [ ] Group B supports bulk review.
- [ ] Group C supports individual approve/reject/override.
- [ ] No proposal is applied before human decision.
- [ ] Final XLSX preserves source workbook columns/order.
- [ ] I/J remain unchanged everywhere.
- [ ] Main output adds only the two configured indicator columns.
- [ ] Summary and attention sheets are produced.
- [ ] Exported file reopens successfully.
- [ ] Re-running exported output produces no new changes.
- [ ] Regression tests exist for `037259`, `185611`, and `172593`.
- [ ] Evaluation framework exists, with current AI-benchmark data gap documented rather than hidden.
- [ ] Open business issues are represented as configuration/safe exceptions, not invented rules.

---

# Appendix A — Current 55 Scope 2b items

These are the current live rows where standardized size/UOM are blank and no legacy UOM exists, therefore normal deterministic rules cannot settle them and description inference is attempted.

| Item No. | Department | English item description | Local item description |
|---|---|---|---|
| `015511` | 03_Grocery 2 | GREEN TEA | 綠茶 |
| `016535` | 03_Grocery 2 | SALAD F SHRIMP CHIP | 法式沙拉味蝦條 |
| `073734` | 03_Grocery 2 | SALT PINEAPPLE | 鹽菠蘿味飲料 |
| `155184` | 03_Grocery 2 | DLX ASST MINI MC | 玲瓏八星月餅 |
| `042275` | 03_Grocery 2 | CAT HFC HA CHCKFLT | 開胃貓鮮燉包雞柳 |
| `171546` | 03_Grocery 2 | TO&GAR MINI CREPES | 番茄乾香蒜迷你薄脆 |
| `171538` | 03_Grocery 2 | BOURSIN MINI CREPES | 蒜蓉香草芝士迷你薄脆 |
| `806869` | 03_Grocery 2 | KINMEDAI | 金目鯛一夜乾 |
| `139022` | 03_Grocery 2 | LASTOSE FREE MOCCA | 零乳糖咖啡朱古力乳酪 |
| `807362` | 03_Grocery 2 | C BREAST CARTILAGE | 雞胸軟骨 |
| `170076` | 03_Grocery 2 | FISHSNACK BK PEPPER | 香脆魚片黑椒味 |
| `170183` | 03_Grocery 2 | C.ROLL LEMON GUMMY | 玉桂狗檸檬味軟糖 |
| `171470` | 03_Grocery 2 | FRUIT & LEMON BIS | 檸檬什果曲奇 |
| `171579` | 03_Grocery 2 | SEASALT MINI CREPES | 香蔥海鹽迷你薄脆 |
| `812131` | 03_Grocery 2 | POK TP MB | 豬肚丸 |
| `083352` | 03_Grocery 2 | HORLICK PISTA CRISP | 好立克開心果脆脆 |
| `084269` | 03_Grocery 2 | DUCK EGG ROLLS | 香港味道-經典鴨蛋卷 |
| `085167` | 03_Grocery 2 | KITTEN CHICKEN DRY | 幼貓雞肉乾糧 |
| `461970` | 03_Grocery 2 | SPECIAL DEL CHG-10 | 特別送貨服務費-10 |
| `078105` | 03_Grocery 2 | CHATEAU MUSAR 2005 | 穆薩紅酒05年 |
| `155259` | 03_Grocery 2 | RB TANG PINE NUT MC | 陳皮豆沙松子月餅 |
| `154047` | 03_Grocery 2 | LAVA CUSTARD MC | 流心奶黃月餅 |
| `460824` | 03_Grocery 2 | SPECIAL DEL CHG-6 | 特別送貨服務費-6 |
| `460907` | 03_Grocery 2 | SPECIAL DEL CHG-7 | 特別送貨服務費-7 |
| `461004` | 03_Grocery 2 | SPECIAL DEL CHG-8 | 特別送貨服務費-8 |
| `142661` | 03_Grocery 2 | LADY NORMAL | LADY護墊多量型 |
| `168203` | 03_Grocery 2 | TUMBLER NAVY | QUENCHER杯藍 |
| `460725` | 03_Grocery 2 | SPECIAL DEL CHG-3 | 特別送貨服務費-3 |
| `147025` | 03_Grocery 2 | CD - SMOOTH & SILKY | 護髮素維他命柔順絲滑 |
| `052787` | 03_Grocery 2 | UME-FLAVOURED TEA | 梅子茶 |
| `043000` | 03_Grocery 2 | GDN BLIND BOX | 造型公仔盲盒 |
| `022657` | 03_Grocery 2 | SHRIMP FLY ROE WT | 手作大蝦飛魚籽雲吞皇 |
| `171892` | 06_Dairy & Frozen | CRACKERS- ORIGINAL | 原味餅乾 |
| `172015` | 06_Dairy & Frozen | CARBONARA CHIPS | 卡邦尼意粉味薯片 |
| `172114` | 06_Dairy & Frozen | DUCK EGG COOKIES | 香港味道-懷舊鴨蛋酥 |
| `172288` | 06_Dairy & Frozen | SPORT ALC FREE BEER | 無酒精啤酒味飲料 |
| `023341` | 06_Dairy & Frozen | PORK&CORN FRIED DP | 煎餃王-豬肉粟米餃 |
| `023366` | 06_Dairy & Frozen | RED BEAN TONG YUEN | 紅豆湯圓 |
| `172593` | 06_Dairy & Frozen | SMALL CAN BEER 4'S | 4 罐裝啤酒 |
| `079095` | 06_Dairy & Frozen | GK ST CHOCO YOGHURT | 黑朱古力希臘乳酪 |
| `081349` | 06_Dairy & Frozen | TOTE BAG | 帆布袋 |
| `165613` | 06_Dairy & Frozen | FRZ JUICY BEEFBALLS | 爆漿撒尿牛丸 |
| `087346` | 06_Dairy & Frozen | CTG MUG CHAMPAGNE | CTG隨行杯香檳 |
| `165266` | 06_Dairy & Frozen | GOLDEN SWEET POTATO | 冰烤黃蕃薯 |
| `165274` | 06_Dairy & Frozen | FISH PASTE | 魚滑 |
| `165324` | 06_Dairy & Frozen | TENDER FISH BALL | 嫩魚丸 |
| `078493` | 06_Dairy & Frozen | FWT 585 TS VERSION | FWT 585 TS |
| `165357` | 06_Dairy & Frozen | CUTTLEFSH FISH BALL | 墨魚味丸 |
| `165639` | 06_Dairy & Frozen | BEEF TENDONBALL | 大顆粒牛筋肉丸 |
| `165605` | 06_Dairy & Frozen | BEEF BALL | 潮汕牛肉丸 |
| `165647` | 06_Dairy & Frozen | MUSHROOM PORK BALLS | 香菇貢丸 |
| `159491` | 06_Dairy & Frozen | VALUE PACK W ECO BG | 優惠套裝連環保袋 |
| `083758` | 06_Dairy & Frozen | HORLICKS EGGROLL | 好立克原味蛋卷 |
| `444786` | 06_Dairy & Frozen | SOCKS BEIGE R/ | 米色短襪換購 |
| `045716` | 06_Dairy & Frozen | HIGH PRO APLYOGHURT | 高蛋白蘋果味乳酪 |

Important: this list is a routing list, **not a statement that AI can successfully populate all 55**. On current data, many have insufficient explicit size/UOM evidence and should correctly end as `NOT_IN_DESCRIPTION` pending Commercial review.

---

# Appendix B — Minimal acceptance fixtures

Use these fixtures in automated tests independent of the full workbook.

## Deterministic

```text
1 KG -> 1000 GM      (if mapping active)
1 LT -> 1000 ML      (if mapping active)
1 PC -> 1 EA         (if mapping active)
unknown unit -> NO_RULE
FZ -> NO_RULE until O-2 is configured
```

## AI explicit evidence

Input:

```text
item_desc_eng = "ORGANIC JUICE 500ML"
```

Expected:

```json
{
  "status": "PROPOSAL",
  "unit_size": 500,
  "uom": "ML",
  "evidence": [{"field":"item_desc_eng","fragment":"500ML"}]
}
```

## AI decline

Input:

```text
item_desc_eng = "GREEN TEA"
```

Expected:

```text
NOT_IN_DESCRIPTION
no size/uom proposal
```

## Multipack

Input:

```text
"4 x 200ML"
```

Expected provisional pack result:

```text
200 / ML / 4
```

## Piece count is not automatically pack size

Input:

```text
"IBN ABL BRAIS SAU(4PCS) 200GM"
```

Expected PRD fixture:

```text
200 / GM / 1
```

## Discrepancy

```text
item_desc_eng = "JUICE 500ML"
item_desc_local = "JUICE 1L"
```

Expected:

```text
discrepancy=true
both values shown
no automatic winner
```

---

# Appendix C — Final implementation principle

Keep this separation throughout the codebase:

```text
Excel Adapter
    ↓
Canonical Product Data
    ↓
Purge + Classification
    ↓
┌───────────────────────────┐
│ Group A: no change        │
│ Group B: deterministic    │
│ Group C: AI interpretation│
└───────────────────────────┘
    ↓
Structured Proposals + Evidence + Flags
    ↓
MongoDB
    ↓
Human Review
    ↓
Final Decisions
    ↓
Excel Writer
    ↓
Corrected XLSX
```

The AI is not the application. It is one narrow component used only where structured source data cannot settle the answer.
