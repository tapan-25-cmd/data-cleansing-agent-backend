# B/C Image Blind Test Plan v1

## Objective

Run an independent image-only test for every Group B and Group C row among the 236
products found on Wellcome. The image reader must produce K (unit size), L (UOM), and
M (pack size) without seeing the workbook answer or the cleansing proposal. Only after
the image response is persisted may a deterministic evaluator compare it with the
values written or proposed by the cleansing pipeline.

This is a corroboration test against product photography. It is not business-labelled
ground truth, so the UI must report **agreement, coverage, conflicts, and abstention**
rather than claim unconditional accuracy.

## Test population

- Start from the completed 236-product Wellcome evidence set.
- Include only source rows classified as Group B or Group C.
- Include all such rows, not a sample.
- Preserve one result per source Excel row even if product numbers repeat.
- Report separately: eligible, product found, images fetched, readable, comparable,
  scored, abstained, source conflict, and technical failure.
- Store a frozen population manifest with the run so later pipeline changes cannot
  silently change the denominator.

The exact B/C count must be calculated from MongoDB at run creation and shown in the
run summary. It must not be hardcoded.

## Strict blindness contract

### The image reader may receive

- all gallery product images successfully fetched for that Wellcome product;
- image order and stable image identifiers;
- a number-stripped product identity hint solely to reject images of another product;
- the output JSON schema and measurement/packaging interpretation instructions.

### The image reader must not receive

- Group B or C;
- existing, proposed, accepted, or final K/L/M;
- legacy I/J values;
- deterministic rule outcome or review status;
- page text, item descriptions, web descriptions, reviewer comments, or calculated
  totals containing quantity evidence.

The request payload should be stored in redacted/auditable form, and an automated test
must prove that none of the prohibited fields can enter it.

## Image-reader result

Persist the response before loading the answer key:

- exact printed quantity text;
- inferred K, L, and M;
- measurement kind;
- packaging interpretation (per unit, outer total, nested count, or unknown);
- evidence image number and image URL/hash;
- total images read and excluded unrelated images;
- confidence with human-readable reason;
- explicit abstention reason;
- prompt, model, reader, and policy versions;
- timestamps, latency, token usage, and technical error details.

If gallery images disagree, the reader must return `IMAGE_SOURCE_CONFLICT`; it must not
select the answer that happens to agree with our workbook.

## Answer keys and comparisons

After the blind response has been saved, load these values separately:

1. Uploaded Excel K/L/M.
2. Cleansing proposal K/L/M, when present.
3. Final K/L/M that the current downloadable workbook will contain.

For Group B, the primary comparison is blind image K/L/M versus final downloadable
K/L/M. For Group C, show two independent comparisons: versus the proposal and versus
the final downloadable value. A C proposal supported by an image is evidence in its
favour, but is not labelled business-approved accuracy.

The deterministic scorer normalizes permitted units and evaluates both representation
and sellable total:

`unit size (K/L) x pack size (M) = total sellable/consumption quantity`

Verdicts:

- `EXACT_KLM_MATCH`
- `EQUIVALENT_TOTAL_DIFFERENT_SPLIT`
- `UNIT_SIZE_OR_UOM_MISMATCH`
- `PACK_SIZE_MISMATCH`
- `TOTAL_QUANTITY_MISMATCH`
- `IMAGE_SOURCE_CONFLICT`
- `OTHER_PRODUCT_IMAGE`
- `NOT_COMPARABLE`
- `NO_READABLE_QUANTITY`
- `TECHNICAL_ERROR`

Exact K/L/M agreement and total-quantity agreement must be reported separately. This
prevents `90 GM x 5` and `450 GM x 1` from being called identical while still showing
that both encode the same total quantity.

## Backend design

- Add dedicated `bc_image_blind_runs` and `bc_image_blind_rows` persistence. Do not
  overwrite website-evidence documents.
- Freeze source row, image hashes, image URLs, reader result, answer-key snapshot, and
  deterministic score in each result.
- Reuse successful image readings only when product/image hashes and reader versions
  are identical. Reconciliation remains row-specific.
- Use bounded concurrency (start at four), per-item error isolation, fixed retry budget,
  resumable runs, and retry only technical failures.
- Correct conflict precedence before scoring: page/image `YES + NO` is a source conflict,
  never confirmed.

Proposed API:

- `POST /api/jobs/{job_id}/bc-image-blind-test/run`
- `GET /api/jobs/{job_id}/bc-image-blind-test`
- `GET /api/jobs/{job_id}/bc-image-blind-test/rows`
- `POST /api/jobs/{job_id}/bc-image-blind-test/retry-failures`

## UI: new tab

Add **Image blind test** beside the current quality/testing tabs.

The page should contain:

- a plain-language statement of what the AI saw and what was hidden;
- a coverage funnel from eligible B/C rows to scored rows;
- separate B and C cards for exact K/L/M agreement, equivalent-total agreement,
  mismatches, safe abstentions, conflicts, and technical failures;
- a filterable Excel-like table showing item, row, group, final/proposed K/L/M, blind
  image K/L/M, verdict, evidence photo, and concise explanation;
- a persistent detail panel showing all photos, highlighting the evidence photo and
  marking excluded unrelated images;
- run status, progress, cost/latency metadata, and retry-failures control.

The UI should use **agreement rate** until business-approved labels exist. A tooltip must
explain that a product photo is independent evidence, not infallible ground truth.

## Downloadable workbook

Add an `Image Blind Test` sheet to the same generated workbook. Do not change the
original data sheet's values because of this test.

The sheet contains a summary block followed by one row per eligible B/C Excel row:

- item number, source Excel row, group;
- uploaded K/L/M, proposed K/L/M, final download K/L/M;
- blind image K/L/M and calculated total quantities;
- verdict, comparable/scored flags, and human-readable reason;
- printed evidence, image number, images read, excluded images;
- clickable product-page and evidence-photo links;
- prompt/model/reader/policy versions and check time.

If no completed test exists, still create the sheet with `Not run` status and guidance;
never silently export an empty result. Increment the export contract/version.

## Verification gates

1. Unit tests for blind payload leakage, all-gallery processing, unit normalization,
   total equivalence, conflict precedence, abstentions, retries, and idempotency.
2. Repository/API integration tests for frozen populations, pagination, resume, and
   isolation from website evidence.
3. Export tests for sheet presence, row counts, values, hyperlinks, and `Not run` state.
4. Frontend tests/build for metrics, filters, explanations, photo panel, and failures.
5. Real smoke run over the full B/C subset of the 236 products.
6. Manually inspect every mismatch/conflict and a stratified sample of matches before
   presenting the result as a quality claim.

## Implementation order

1. Fix website source-conflict precedence.
2. Add frozen run/result models and deterministic scorer.
3. Add persistence, APIs, retries, and backend tests.
4. Run the real B/C smoke test and audit mismatches.
5. Add the workbook sheet and export tests.
6. Add the UI tab and frontend tests/build.

