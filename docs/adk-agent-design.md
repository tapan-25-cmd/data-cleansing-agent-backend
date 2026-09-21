# Real ADK Agent Design — UoM Description Inference

## 1. Decision

Build one real Google ADK `LlmAgent` named `uom_description_inference_agent` for
Group C measurement extraction and selective B/C pack fallback.

The agent is an evidence extractor. It does not own conversion rules, write workbook
cells, call external systems, browse the web, or decide whether a proposal is accepted.

```text
Allowed product text
  -> ADK extraction agent
  -> observed size/UOM/pack evidence
  -> backend schema and evidence validation
  -> deterministic RuleRegistry normalization
  -> proposal in MongoDB
  -> Commercial review
  -> Excel export
```

This is a real ADK agent even though it has no tools. ADK provides the `LlmAgent`,
Gemini model adapter, structured input/output schemas, runner, isolated sessions,
events, retry configuration, evaluation, and tracing. Tools are deliberately omitted
because R1 forbids web search, external product lookups, and model-driven calculations.

## 2. Planned files

```text
backend/app/agents/
├── provider.py                    # framework-neutral request/result contract
├── factory.py                     # selects mock or ADK; validates configuration
├── mock_provider.py               # deterministic tests/local fallback
├── adk_provider.py                # Runner/session/event adapter
├── uom_inference_agent.py         # real LlmAgent + ADK App definition
└── prompts/
    └── uom_inference_v2.md        # versioned system instruction

backend/tests/
├── unit/
│   ├── test_agent_factory.py
│   ├── test_adk_response_validation.py
│   └── test_prompt_contract.py
├── integration/
│   └── test_adk_provider.py       # recorded/fake model; no live credentials
└── eval/
    ├── uom_inference.evalset.json
    └── test_live_adk_eval.py      # opt-in, billed test
```

The ADK definition belongs in `uom_inference_agent.py`. The FastAPI application must
never construct an ADK agent directly; it receives an `InferenceProvider` from
`factory.py`.

## 3. Agent input boundary

The input model contains a task discriminator, optional known measurement context for
`PACK_ONLY`, and these optional product-text fields:

```text
item_brand_eng
item_brand_local_lang
item_desc_eng
item_desc_local_lang
web_description_eng
web_description_chi
```

The input schema uses `extra="forbid"`. It rejects item identifiers, hierarchy,
legacy I/J, workbook rows, file paths, URLs, and arbitrary metadata. Known measurement
context is backend-generated and cannot be returned or modified by a pack-only result.

Serialize the validated Pydantic model to one JSON user message. Do not build prompts
by concatenating unlabeled values.

## 4. Structured agent output

The ADK `LlmAgent` uses a Pydantic `output_schema` and has no tools. The result is an
observation, not a final standardized value:

```python
class ObservedMeasurement(BaseModel):
    value: Decimal
    uom: str
    field: AllowedEvidenceField
    fragment: str


class AdkInferenceOutput(BaseModel):
    status: Literal[
        "PROPOSAL",
        "PACK_PROPOSAL",
        "NOT_IN_DESCRIPTION",
        "AMBIGUOUS",
        "CONFLICT",
    ]
    measurement: ObservedMeasurement | None
    pack_size: Decimal | None
    pack_evidence: Evidence | None
    conflicting_measurements: list[ObservedMeasurement]
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    reason_code: str
```

The agent reports the value and unit exactly as observed. For example, `1 KG` remains
`1 / KG` in the agent response. The backend applies `KG_TO_GM`; the model must not
calculate `1000 GM`.

Use `output_key="uom_inference_result"`. Treat the final ADK event as untrusted even
when ADK has applied the schema: parse it again through the application Pydantic model.

## 5. Agent definition

`uom_inference_agent.py` will define:

```python
root_agent = LlmAgent(
    name="uom_description_inference_agent",
    description="Extracts explicit unit-size and pack evidence from permitted product text.",
    model=Gemini(
        model=settings.gemini_model,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=load_versioned_prompt("uom_inference_v2.md"),
    input_schema=InferenceRequest,
    output_schema=AdkInferenceOutput,
    output_key="uom_inference_result",
    generate_content_config=types.GenerateContentConfig(
        temperature=0,
        max_output_tokens=800,
    ),
    tools=[],
)

adk_app = App(name="uom_cleansing", root_agent=root_agent)
```

The exact deployable Gemini model id is required configuration and is pinned per
environment. Do not use an implicit/default model in benchmark or production runs.

## 6. Prompt contract

The versioned instruction must state:

- extract only literally present evidence;
- never use product or category knowledge;
- never guess a common package size;
- never convert units or perform arithmetic;
- consider all six fields jointly without a fixed language preference;
- cite the exact source field and exact substring;
- distinguish per-unit size from pack count;
- return `NOT_IN_DESCRIPTION` when evidence is absent;
- return `AMBIGUOUS` when a signal cannot safely be interpreted;
- return `CONFLICT` with every relevant observation when fields disagree;
- ignore years, model numbers, charge codes, and version identifiers unless they have
  explicit measurement-unit evidence;
- return only the structured output.

Prompt text, examples, output schema, and model configuration are reviewed as code.
Each prompt change increments `PROMPT_VERSION` and updates the evaluation set.

## 7. ADK runtime adapter

`AdkInferenceProvider` owns a `Runner` and `InMemorySessionService` for the POC.

For each item:

1. Generate a fresh session id. Never reuse a session across products.
2. Create the session with a constant non-personal worker user id.
3. Serialize `InferenceRequest` as JSON into `types.Content`.
4. Iterate `runner.run_async(...)` events.
5. Accept only the final response event.
6. Parse and validate the structured response.
7. Destroy or abandon the one-item session after the call.
8. Return the framework-neutral `InferenceResult` to `JobProcessor`.

Fresh sessions prevent one product's descriptions from influencing another. ADK state,
memory, artifacts, and conversation history are not business storage; MongoDB remains
the durable proposal store.

## 8. Application-side validation

After every ADK response, enforce all of the following outside the model:

1. Every evidence field is on the six-field allowlist.
2. Every cited fragment occurs literally in the named input field.
3. `PROPOSAL` contains a positive observed value and a supported source UOM.
4. `NOT_IN_DESCRIPTION` contains no measurement or pack proposal.
5. `CONFLICT` contains at least two conflicting observations.
6. Pack size is positive and is supported by separate literal evidence.
7. The observed UOM resolves through `RuleRegistry`; otherwise route to `NO_RULE`.
8. Standardized output UOM is produced only by `RuleEngine` and is one of
   `EA`, `GM`, `ML`, `FT`.
9. `PACK_ONLY` cannot return measurement observations and pack evidence cannot cite brand fields.
10. Any invalid response becomes `AI_INVALID_RESPONSE` or `PACK_AGENT_ERROR` without stopping other rows.

The ADK agent never writes MongoDB or Excel directly.

## 9. Provider selection and startup behavior

`factory.py` implements:

```text
AI_PROVIDER=mock -> MockInferenceProvider
AI_PROVIDER=adk  -> AdkInferenceProvider
anything else    -> startup failure
```

When `AI_PROVIDER=adk`, startup fails clearly if any required setting or credential is
missing. Do not silently fall back to mock in a deployed environment.

Required Gemini Developer API configuration:

```dotenv
AI_PROVIDER=adk
GEMINI_API_KEY=
GEMINI_MODEL=
```

No Google Cloud project or Application Default Credentials are required. The API key
must remain in the gitignored `.env` or an external secret manager and must never be
committed to the repository.

## 10. Concurrency, retries, and failure isolation

- Keep the existing application semaphore at `AI_MAX_CONCURRENCY` (default 5).
- Configure three total model attempts for transient provider failures.
- Add exponential backoff with jitter around the complete ADK invocation.
- Do not retry schema-invalid or evidence-invalid output endlessly.
- Apply a configurable per-item timeout.
- After exhaustion, persist `AI_PROVIDER_ERROR` and continue the job.
- Never send Group A or Group B unit conversion through the provider. Only unresolved
  pack clues may create a Group B `PACK_ONLY` call.

## 11. Provenance and observability

Persist on each AI proposal:

```text
provider = google-adk
agent_name
agent_version
prompt_version
prompt_sha256
model_id
ADK version
provider request/trace id when available
attempt count
latency_ms
input field names sent (not full text in ordinary logs)
validation outcome
```

Persist these values on the job as well. Logs must not contain full workbook rows or
credentials. Content logging is off by default.

## 12. Test and evaluation plan

### Offline tests required in CI

- provider factory selects the configured provider;
- forbidden input keys are rejected before ADK invocation;
- prompt checksum/version is stable;
- final event extraction works with recorded/fake ADK events;
- missing final event becomes a provider error;
- malformed structured output is rejected;
- fabricated evidence is rejected;
- observed `1 KG` is converted by `RuleEngine`, not by the agent;
- fresh session id is used for every row;
- concurrency never exceeds the configured limit;
- B rows never call the ADK provider.

### Curated evaluation cases

Include explicit English evidence, explicit local-language evidence, equivalent bilingual
evidence, conflicts, multipacks, piece-count negatives, years/model numbers, service
charge codes, unknown units, and no-evidence descriptions.

Live evaluations are opt-in because they use credentials and incur cost. Record the
model id, prompt version, dataset version, and result artifacts for every benchmark.

## 13. Delivery sequence

Steps 1–5 are implemented in the codebase. Steps 6–9 require a valid Gemini API key,
an approved model id, and explicit rollout approval because they make live model calls.

1. Finalize `AdkInferenceOutput` and prompt fixtures.
2. Add ADK 2.x dependency and provider factory.
3. Implement the agent definition and ADK runtime adapter.
4. Add fake-event integration tests.
5. Add Gemini Developer API-key configuration and startup checks.
6. Run a credentialed smoke test against a tiny synthetic dataset.
7. Run the curated evaluation set and review failures.
8. Enable ADK for the 55 current Group C rows only.
9. Confirm cost, latency, decline rate, and evidence validity before UAT.

## 14. Acceptance gates

The real agent is ready only when:

- the ADK provider is selected through configuration;
- the mock remains available for deterministic CI;
- no forbidden field reaches an ADK request;
- no ADK tools, web access, or external lookup capability is attached;
- all accepted evidence is literal and backend-validated;
- conversions are performed only by deterministic code;
- invalid/failed calls safely become review exceptions;
- the curated evaluation result is reported separately from deterministic accuracy;
- a full run proves Group A/B invoke ADK zero times.
