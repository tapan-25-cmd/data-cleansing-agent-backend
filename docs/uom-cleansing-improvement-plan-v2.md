# UoM Cleansing Improvement Plan v2

**Status:** Proposed; no business-policy changes in this document are active yet.  
**Date:** 21 September 2026  
**Scope:** Explainable result ledger, human-in-the-loop review, packaging intelligence, discrepancy detection, and measurable accuracy.

## 1. Outcome

Keep the existing chat upload and processing experience. After processing completes, add a **View detailed results** button that opens a full-page, Excel-like result ledger in the same application shell. The ledger must show every selected row, what changed or did not change, why, the evidence used, the responsible method, and whether a human decision is required. A second tab must show versioned business test cases and accuracy results.

The upgraded decision boundary is:

```text
agent extracts explicit observations
        -> deterministic resolver generates and checks candidate interpretations
        -> policy decides AUTO_APPLY, REVIEW_REQUIRED, OBSERVATION_ONLY, or NO_CHANGE
        -> human may accept, reject, or override review-required candidates
        -> export writes approved/automatic K/L/M changes plus audit columns
```

The LLM must not invent packaging arithmetic, choose an unapproved hierarchy, or directly mutate K/L/M.

## 2. Findings from the real v0.2 job

The latest exported job contains 13,300 selected rows, 12,542 live rows, 11,975 A rows, 512 B rows, and 55 C rows.

### 2.1 Group A warnings

There are 544 distinct validated A rows with non-blocking warnings:

| Warning combination | Rows |
|---|---:|
| Legacy size mismatch only | 323 |
| Legacy UOM mismatch only | 207 |
| Description measurement mismatch only | 11 |
| Description and legacy-size mismatch | 3 |

These are observations, not 544 proven errors. They mix rounding differences, source-field semantic differences, packaging-level differences, and possible genuine data errors.

### 2.2 Pack-size outcomes

For Group B, 342 existing M values were preserved, 154 rows had no pack evidence, 16 went to pack-only inference, nine received agent proposals, and seven were declined. All nine proposals were Group B results; seven were based on `1PC`, one on `12PCS`, and one on `12S`. Group C produced no K/L/M proposals because none of its 55 rows contained an explicit measurement in the permitted text; one row contained only the pack clue `4'S`.

### 2.3 Packaging hierarchy examples

The current first-signal discrepancy logic cannot explain these records:

| Item | Text | Existing K/L/M | Observation |
|---|---|---|---|
| `241315` | `5 CASE/10 X 90GM` | `18 / GM / 50` | `90 / 5 = 18`, `5 * 10 = 50` |
| `241448` | `5 CASE/10 X 90GM` | `18 / GM / 50` | same pattern |
| `240929` | `5 CASE/6 X 90GM` | `450 / GM / 6` | `5 * 90 = 450`, inner count retained as M |
| `240572` | `4 CASE/16 X 200GM` | `800 / GM / 16` | `4 * 200 = 800`, inner count retained as M |
| `241547` | `CASE/12 X 185GM` | `185 / GM / 12` | ordinary per-unit size and count |

Similar syntax has different existing interpretations. The two KIKI rows are also exceptional among nearby case-offer items. They must become labelled business test cases before automation; they are not sufficient evidence for a general division rule.

### 2.4 Group C evidence coverage

In the real 55 C rows:

- zero explicit measurement signals occur in brand fields;
- zero explicit measurement signals occur in description fields;
- one description contains a pack clue (`SMALL CAN BEER 4'S`) without a size;
- all 55 correctly abstained under the current evidence-only contract.

More prompt intelligence cannot recover facts absent from the supplied workbook. These rows need enriched source data or human input, not guessing.

## 3. Business decisions required before implementation

1. Define K precisely: per consumer unit, per sellable trade item, total of an inner bundle, or another level.
2. Define M precisely: immediate child count, physical piece count, sellable units, or case quantity.
3. Confirm packaging hierarchy semantics for `CASE/N`, `A CASE/B X C`, `PCS`, `S`, coupons, vouchers, and gift boxes.
4. Confirm whether explicit `1PC` should populate blank M with `1`.
5. Confirm whether `12S` is acceptable evidence for `12`.
6. Approve mismatch tolerances by dimension and conversion family.
7. Decide which finding categories may auto-apply and which always require a human.
8. Confirm whether legacy I/J is authoritative enough to override existing K/L when the difference is material. A large difference alone proves disagreement, not which source is correct.

## 4. Result and review model

Replace the single reason string as the primary explanation with structured findings and field-level changes.

```json
{
  "findings": [
    {
      "code": "SIGNIFICANT_LEGACY_SIZE_MISMATCH",
      "category": "SOURCE_DISCREPANCY",
      "severity": "REVIEW",
      "title": "Standardized size differs materially from legacy conversion",
      "human_reason": "Existing value is 375 GM; 12.3 OZ converts to 349 GM.",
      "evidence": [],
      "policy_version": "mismatch-policy-v2"
    }
  ],
  "changes": [
    {
      "field": "standard_size",
      "original": "375",
      "proposed": "349",
      "final": "375",
      "action": "REVIEW_REQUIRED",
      "method": "RULE",
      "rule_id": "OZ_TO_GM",
      "confidence": "HIGH"
    }
  ],
  "review": {
    "status": "PENDING",
    "decision": null,
    "actor": null,
    "decided_at": null,
    "comment": null
  }
}
```

### 4.1 Action states

| State | Meaning | K/L/M mutation |
|---|---|---|
| `NO_CHANGE` | Valid and consistent | None |
| `AUTO_APPLY` | Approved deterministic correction | Apply automatically |
| `REVIEW_REQUIRED` | Plausible proposal but business judgment required | Do not apply until accepted |
| `OBSERVATION_ONLY` | Discrepancy exists but no defensible proposal | None |
| `UNRESOLVED` | Missing rule/evidence/provider result | None |
| `REJECTED` | Human rejected proposal | None |
| `OVERRIDDEN` | Human supplied final K/L/M | Apply override |

Review remains optional for downloading automatic results, but pending review proposals must not silently change K/L/M.

### 4.2 Issue taxonomy

- `VALID_NO_CHANGE`
- `CANONICAL_NORMALIZATION`
- `DETERMINISTIC_UNIT_CONVERSION`
- `ROUNDING_ONLY_VARIANCE`
- `SIGNIFICANT_LEGACY_SIZE_MISMATCH`
- `LEGACY_UOM_DIMENSION_MISMATCH`
- `DESCRIPTION_MEASUREMENT_MISMATCH`
- `CROSS_LANGUAGE_CONFLICT`
- `CROSS_SOURCE_CONFLICT`
- `PACKAGING_HIERARCHY_AMBIGUOUS`
- `PACK_COUNT_CONFLICT`
- `PACK_EVIDENCE_INSUFFICIENT`
- `UNMAPPED_SOURCE_UOM`
- `INVALID_EXISTING_VALUE`
- `AGENT_ABSTAINED`
- `AGENT_ERROR`
- `PURGED_ROW`

### 4.3 Color policy

Use color for action/risk, not for A/B/C or AI versus rules:

| Color | Meaning |
|---|---|
| Blue/neutral | Valid or informational |
| Green | Automatic change applied |
| Amber | Human review required |
| Red | Conflict, invalid data, or blocked proposal |
| Grey | Purged, no evidence, or unresolved |

Always display a text label and icon; color alone is not accessible evidence.

## 5. Output workbook additions

Append audit columns at the end of the primary worksheet without changing source columns:

1. `Cleansing Status`
2. `Issue Category`
3. `Changed Fields`
4. `Original K`
5. `Original L`
6. `Original M`
7. `Proposed K`
8. `Proposed L`
9. `Proposed M`
10. `Final K`
11. `Final L`
12. `Final M`
13. `Human-readable Reason`
14. `Evidence Field`
15. `Evidence Fragment`
16. `Method`
17. `Rule / Prompt Version`
18. `Confidence`
19. `Review Required`
20. `Review Decision`
21. `Reviewer Comment`

Use semicolon-separated values when a row has multiple findings/evidence fragments. Add an `UoM_Cleansing_Summary` sheet with counts by action, issue category, method, and review status. Do not place raw provider errors or sensitive configuration in the workbook.

## 6. Rounding and mismatch policy

### 6.1 Identity comparisons

For `GM -> GM`, `ML -> ML`, `EA -> EA`, and `FT -> FT`, compare exact decimals. Do not apply Group B nearest-whole rounding. This removes the false warning for item `119321` (`4.5 GM` versus `4.5 GM`).

### 6.2 Converted comparisons

Store raw converted value, rounded candidate, absolute difference, relative difference, and tolerance outcome. Proposed initial categories, pending business approval:

- exact match: no finding;
- within approved rounding tolerance: `ROUNDING_ONLY_VARIANCE`, no change;
- outside tolerance: `SIGNIFICANT_LEGACY_SIZE_MISMATCH`, review required;
- different physical dimension: `LEGACY_UOM_DIMENSION_MISMATCH`, observation/review only.

Item `628776` (`484 GM` versus calculated `485 GM`) should be rounding-only and unchanged. Item `044305` (`375 GM` versus calculated `349 GM`) should show proposed `349 GM` for review. It should auto-override only if the business explicitly designates legacy I/J as authoritative under this policy.

## 7. Packaging intelligence v2

### 7.1 Structured extraction

Replace a single `pack_size` observation with a packaging-expression contract:

```json
{
  "measurements": [
    {"value": "90", "uom": "GM", "field": "web_description_eng", "fragment": "90GM"}
  ],
  "counts": [
    {"value": 5, "qualifier": "CASE_PREFIX", "fragment": "5 CASE"},
    {"value": 10, "qualifier": "INNER_COUNT", "fragment": "/10 X"}
  ],
  "operators": ["CASE_SLASH", "MULTIPLY"],
  "packaging_terms": ["CASE"],
  "status": "OBSERVATIONS_ONLY"
}
```

The agent extracts literal observations and likely grammatical roles; it does not calculate K or M.

### 7.2 Deterministic candidate resolver

Generate named candidates from extracted facts:

- per-unit: `K = measurement`, `M = inner_count`;
- outer-weight: `K = outer_count * measurement`, `M = inner_count`;
- nested-count: `M = outer_count * inner_count`;
- business-specific allocation: `K = measurement / outer_count`, only if an approved policy explicitly permits it.

Validate each candidate against legacy I/J, existing K/L/M, package terminology, section policy, and cross-language evidence. If exactly one approved interpretation survives, propose it. If multiple survive, create `PACKAGING_HIERARCHY_AMBIGUOUS` and require review.

Do not learn a global arithmetic rule from items `241315` and `241448`; create a narrowly scoped policy only after business owners confirm why their existing values are correct and which source fields identify that scope.

### 7.3 M decision order

1. Preserve valid existing M.
2. Normalize numeric-text M without changing value.
3. Extract all counts, measurements, operators, and package terms from every description field.
4. Build packaging-level candidates; do not select a count merely because it is consistent across fields.
5. Check agreement with K/L, legacy I/J, section policy, and bilingual descriptions.
6. Auto-apply only an approved, unambiguous pattern.
7. Use the agent for unresolved grammatical roles, not undocumented arithmetic.
8. Require human review for coupon/voucher counts, `1PC`, `12S`, loose `PCS`, and multi-level package expressions until policies are approved.
9. Leave M blank on no evidence, unresolved ambiguity, conflict, invalid response, or provider error.

### 7.4 Brand-field policy

Brand fields are currently excluded from **pack-count evidence** because numbers in brands and product families can be model/version/name tokens rather than quantities. The real 55 C rows contain no measurement signal in brand fields, so including them would not recover current C values.

Revised policy:

- allow brand fields as weak entity/context input for the combined C task;
- allow brand/description pair comparison for discrepancy detection;
- do not allow a brand fragment alone to authorize K/L/M;
- allow brand numerical evidence only after a separate labelled evaluation proves a specific, approved syntax reliable;
- retain the backend validator that rejects brand-only pack evidence.

## 8. Discrepancy engine v2

The current engine extracts only the first measurement from each field and compares only within three bilingual pairs. Replace it with:

1. **Multi-signal extraction:** return every measurement, count, and package expression with offsets and normalized values.
2. **Pair comparison:** compare English/local brand, item description, and web description independently.
3. **Cross-source comparison:** compare item description to web description and all explicit facts to legacy and K/L/M.
4. **Tolerance-aware equivalence:** treat approved conversion/rounding tolerances as equivalent.
5. **Packaging-aware comparison:** distinguish per-unit, inner-pack, outer-case, and total measurements.
6. **Missing-side state:** one populated side is `INSUFFICIENT_COMPARISON_DATA`, not a conflict.
7. **Conflict classification:** dimension conflict, value conflict, count conflict, hierarchy ambiguity, or likely typo.
8. **Evidence preservation:** store every literal fragment and normalized interpretation used.

No discrepancy alone should overwrite K/L/M. It should either support an independently defensible deterministic proposal or become a review finding.

## 9. Backend plan

### 9.1 Domain and persistence

- Add `Finding`, `FieldChange`, `EvidenceRef`, `PackagingExpression`, `InterpretationCandidate`, and `ReviewDecision` models.
- Preserve immutable original K/L/M, proposed K/L/M, and final K/L/M separately.
- Version every validator, extractor, resolver, tolerance policy, ruleset, agent, and prompt.
- Add MongoDB indexes for job, action, issue category, severity, review status, changed field, method, item number, and row number.

### 9.2 Services

- `MeasurementSignalExtractorV2`: all signals, aliases, positions, and normalized units.
- `PackagingExpressionExtractor`: deterministic high-precision grammar first.
- `PackagingResolver`: candidate arithmetic and policy validation.
- `MismatchPolicy`: exact identity comparison plus configurable conversion tolerances.
- `FindingBuilder`: stable codes and plain-language reasons.
- `ReviewService`: accept, reject, override, comment, actor, timestamp, and audit history.
- `EvaluationService`: run versioned golden cases and persist metrics.

### 9.3 APIs

- `GET /jobs/{id}/results` with server-side pagination, search, sort, and filters.
- `GET /jobs/{id}/result-facets` for filter counts.
- `GET /jobs/{id}/items/{row}` for full evidence and interpretation candidates.
- `PATCH /jobs/{id}/items/{row}/decision` for accept/reject/override.
- `POST /jobs/{id}/decisions/bulk` only for identical rule/category groups.
- `GET /evaluations/latest` and `GET /evaluations/{run_id}/cases`.
- `POST /evaluations/run` restricted to authorized users/environments.

### 9.4 Export

- Extend the XML patch exporter to append audit columns and a summary sheet.
- Apply `AUTO_APPLY`, accepted, and overridden values only.
- Preserve K/L/M for pending, observation-only, rejected, and unresolved rows.
- Include pending-review counts in the summary but do not block download.

## 10. Frontend plan

Keep the chat page unchanged except for a post-processing **View detailed results** button.

### 10.1 Detailed results page

Route: `/jobs/:jobId/results`

Use the same sidebar/header layout. Provide two tabs:

1. **Results ledger**
2. **Accuracy & test cases**

The results ledger should use a virtualized or server-paginated table with frozen item number and K/L/M columns. Columns should mirror the workbook audit fields. Add search and filters for action, issue category, severity, A/B/C, method, changed field, UOM, confidence, and review status.

Selecting a row opens a right detail drawer containing:

- source hierarchy and descriptions;
- original, proposed, and final K/L/M;
- human-readable reason;
- evidence fragments highlighted in their source fields;
- conversion math and rounding/tolerance calculation;
- alternative packaging interpretations;
- rule/prompt/model versions;
- Accept, Reject, and Override actions only when review is required.

### 10.2 Usability rules

- Default view: changed or review-required rows first, with an option for all rows.
- Never show internal enum codes without human labels.
- Show `No change` explicitly instead of empty proposal cells.
- Keep risk/action color separate from A/B/C and method.
- Support column selection, density controls, CSV/XLSX audit export, and deep-linkable filters.

## 11. Accuracy and test-case screen

Do not display “accuracy” from production agreement with existing K/L/M; existing values are not automatically truth. Build a business-labelled golden dataset.

### 11.1 Test-case sources

- confirmed correct A rows;
- rounding boundary cases;
- material legacy mismatches;
- every deterministic conversion rule;
- unknown UOMs;
- explicit single-level packs;
- multi-level case/inner packs;
- coupons/vouchers and `1PC`;
- bilingual agreement and conflict;
- missing evidence and correct abstention;
- malformed values and formulas;
- every production defect converted into a regression case.

Include items `628776`, `044305`, `119321`, `241315`, `241448`, `240929`, and `240572` after business labels are approved.

### 11.2 Metrics

- K exact-match accuracy;
- L exact-match accuracy;
- M exact-match accuracy;
- complete-row exact match;
- proposal precision and recall by field;
- false auto-change rate;
- review routing precision/recall;
- abstention correctness;
- evidence-grounding rate;
- deterministic rule coverage;
- agent schema-validity/error rate;
- accuracy sliced by category, rule, pattern, language, and packaging level;
- latency and model-call count as operational metrics, not accuracy.

Show the labelled-case count and confidence interval/context beside every percentage. A test-case table must show expected, actual, pass/fail, reason, evidence, method, and version.

Google ADK supports versioned evaluation datasets and metrics such as instruction following, grounding, hallucination, and reference matching; use those for the extraction agent, while exact business-field scoring remains deterministic in our own evaluator. See the [ADK evaluation guide](https://google.github.io/agents-cli/guide/evaluation/).

## 12. Testing plan

1. Unit tests for all extractors, arithmetic candidates, tolerances, taxonomy, and human reasons.
2. Property tests for unit conversion and rounding invariants.
3. Contract tests ensuring agents cannot write final fields or cite absent evidence.
4. Golden tests for all approved business cases.
5. Integration tests covering review decisions through export.
6. UI tests for filtering, detail evidence, decisions, and accuracy tabs.
7. Snapshot regression against v0.2 counts, with intentional-change approval.
8. Opt-in real-agent evals with redacted/approved data and fixed prompt/model versions.

Release gates:

- zero unsupported output UOMs;
- zero silent M defaults;
- zero brand-only pack proposals;
- zero auto-applied review-required changes;
- 100% evidence-fragment validation;
- false auto-change rate below the business-approved threshold;
- all critical golden cases pass.

## 13. Delivery phases

### Phase 0 — Business definitions and labels

Approve K/M semantics, tolerances, authority order, packaging expressions, and the initial golden cases. No new auto-overrides before this phase is complete.

### Phase 1 — Explainable ledger

Add findings/changes/review data, APIs, detailed results page, filters, detail drawer, audit columns, and summary sheet. Keep current processing decisions unchanged.

### Phase 2 — Correct comparison behavior

Fix exact identity comparison, add tolerances, multi-signal discrepancy extraction, categories, and human-readable reasons. Route material mismatches to review.

### Phase 3 — Packaging intelligence v2

Add structured packaging observations, deterministic candidate resolver, agent v3 schema/prompt, and the approved case hierarchy policies.

### Phase 4 — Evaluation product

Add golden dataset management, automated evaluation runs, accuracy/test-case UI, slice metrics, and release gates.

### Phase 5 — Controlled expansion

Use reviewed production decisions to propose new deterministic rules. Promotion requires enough labelled examples, business approval, regression tests, and a new version; do not train or change runtime behavior directly from a single reviewer action.

## 14. Governance rationale

NIST recommends explicitly defining human/AI roles, documenting knowledge limits and oversight, and measuring systems against representative benchmarks. Our review states, evidence ledger, versioned policies, golden datasets, and override audit follow that approach. See the [NIST AI RMF Core](https://airc.nist.gov/airmf-resources/airmf/5-sec-core/) and [human-AI interaction guidance](https://airc.nist.gov/airmf-resources/airmf/appendices/app-c-ai-risk-management-and-human-ai-interaction/).

Packaging must be modeled by hierarchy rather than one undifferentiated count. GS1 distinguishes packaging levels and calls for the quantity of the next lower-level trade item at each hierarchy level; it also distinguishes declared net content from hierarchy quantity. See the [GS1 GDSN Trade Item Implementation Guide](https://www.gs1.org/docs/gdsn/3.1/GDSN_Trade_Item_Implementation_Guide.pdf) and [GS1 declared net content definition](https://www.gs1.org/1/gtinrules/en/rule/266/declared-net-content).

## 15. Recommended immediate decisions

1. Approve the detailed ledger and audit-column contract independently of new inference rules.
2. Change identity comparison to exact decimals.
3. Label `044305` as either review-only or approved auto-override; do not infer authority from magnitude alone.
4. Confirm the intended arithmetic and product hierarchy for the four multi-level noodle examples.
5. Keep `1PC`, `12S`, coupons, and loose piece counts review-required until explicitly approved.
6. Build the first golden dataset before changing the agent prompt.
