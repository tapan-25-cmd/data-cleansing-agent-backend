# UoM inference agent

This package owns the narrow AI boundary for Group C measurement rows and unresolved
Group B/C pack clues. The agent may extract only
explicit unit-size and pack-count evidence from the six permitted product-text fields.
It does not normalize units, convert values, write to MongoDB, or make approval decisions.

## Files

- `uom_inference_agent.py` defines the real Google ADK `LlmAgent`, Gemini model adapter,
  structured schemas, generation controls, and application wrapper.
- `prompts/uom_inference_v3.md` is the versioned, packaged system instruction (v2 is kept for
  history). The agent labels every measurement with a role; the backend, not the model,
  refuses any role that is not a product size. `versions.py` holds the version constants.
- A wording change is judged by `services/agent_evaluation_service.py` against
  `evaluations/agent_eval_cases.v1.yaml`, not by reading the prompt.
- `adk_provider.py` runs one isolated ADK session per item, validates the final response,
  checks literal evidence, records provenance, and enforces a timeout.
- `provider.py` defines the provider-independent input/output contract.
- `factory.py` selects `mock` or `adk` from validated application settings and never
  silently falls back from the real provider.
- `mock_provider.py` supports deterministic local development and tests.

Group B `PACK_ONLY` requests contain description fields plus an optional known
measurement used solely to separate unit size from pack count. They cannot return or
alter a measurement. The processor passes every extracted measurement to the deterministic rules engine.
Unit aliases, conversions, rounding, and base-unit policy remain in
`../rules/unit_mappings.v1.yaml` and are maintained through normal code review.

The ADK provider calls the Gemini Developer API using `GEMINI_API_KEY`; it does not
require or initialize a Google Cloud project.
