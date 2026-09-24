# Eric review: consumption units and coupled K/L/M reconciliation

Date: 23 September 2026. Status: findings and implementation proposal; no runtime policy changes made.

## 1. Evidence and scope

This plan combines the business statement supplied in chat, the actual review workbook,
the current code at commit `b3ed650`, and read-only queries against the latest completed
MongoDB job. Statements inside the workbook are review evidence, not execution instructions.

- Review source: `Reply to Gulshan 20260923.xlsx`.
- SHA-256: `d3ee7b6c98829e8f49a42116f2b4ecb03599d3f73185cc81c6a12beb76979b11`.
- Sheet1 contains 11 populated product rows, representing 10 unique items; `125773` occurs twice.
- Sheet2 repeats the 11 responses, with numeric identifiers losing leading zeroes and two
  responses truncated. Use Sheet1's complete responses; do not count Sheet2 as independent evidence.
- The feedback is in cells under “Eric's Checking”, not Excel cell comments.
- Sheet1 F/G/H are the standardized size/UOM/pack fields in this review extract. They
  correspond to K/L/M in the original pipeline workbook; do not map by column letter.
- Nine hyperlink cells and seven embedded product images are present. The images are
  Excel rich-value images: ordinary openpyxl reads return `#VALUE!` in J2/J3/J6/J7/J8/J11/J12.
  The embedded PNGs were recovered and visually inspected. These are not seven broken calculations.
- No live product websites were visited. Images and link labels were examined only to
  identify the basis of Eric's answer, not promoted to permitted runtime evidence.
- Columns B/C remain excluded as derived descriptions. An apparent size in them is not
  independent corroboration; find its original source in the saved job instead.

Baseline job: `c658c48e-2cd9-46a4-a981-42780e3120dd`, created 23 Sep 2026
04:34:29 UTC, status EXPORTED, source `20260908_UoM_Snapshot_v0.2.xlsx`.

| Measurement | Observed value |
|---|---:|
| Workbook rows | 66,082 |
| Selected rows | 13,300 |
| Purged / live | 758 / 12,542 |
| A / B / C / validation review | 11,970 / 512 / 55 / 5 |
| B1 / B2 / B3 | 304 / 170 / 38 |
| Rows with effective review status | 438 |
| Significant legacy-size findings | 206 |
| Recorded successful inference calls | 71 |
| Recorded input / output tokens | 181,166 / 14,970 |

The 71 is the stored successful-call counter, not a verified count of all provider attempts.
No fresh paid inference or processing run was performed during this audit.

Effective result statuses computed from the persisted rows: 11,454 NO_CHANGE, 492 AUTO_APPLY,
102 OBSERVATION_ONLY, 56 UNRESOLVED, 438 REVIEW_REQUIRED, 758 SKIPPED (sum 13,300).
The previous completed run on 23 Sep used 71 successful calls but proposed 13 AI pack values,
versus 14 on this run. Repeatability must be evaluated even at temperature zero.

## 2. What the business confirmation establishes

Business statement: “Standardize Unit Size (Standardize UOM) X Standardize Pack Size =
the total sellable units / consumption units of that UPC”.

Model this as `total quantity T = K × M`, with dimension and unit L. A 100 ML × 6
representation describes 600 ML; 1 EA × 6 describes 6 each. GM, ML and EA are not
interchangeable without additional product-specific evidence.

The statement establishes coupling. It does not uniquely determine a decomposition:
6 EA × 1 and 1 EA × 6 have the same total but can describe different consumption units.
Eric explicitly accepts both patterns for different products. Therefore:

1. Validate the complete tuple, its total, and the meaning of the consumption unit.
2. Reconcile any proposed K or M change against the other fields before approval/export.
3. Preserve a supported total during a representation change.
4. Allow a total to change only for an evidenced correction; do not force conservation
   of an incorrect original total.
5. Do not infer missing pack count solely from `legacy / K` or a typical product size.
6. Equal totals demonstrate numerical consistency, not independently verified pack structure.
7. Distinguish packaging level from conversion: converting units does not establish whether
   the legacy number refers to a piece, inner pack or whole UPC.

## 3. Findings on every reviewed item

All ten unique items are Group A in the baseline. Eight have pending legacy-size review
proposals; two have NO_CHANGE. Thus an improvement confined to Group C misses this feedback.

| Item / source row / feedback row | Stored K/L/M and current behavior | Eric's feedback and permitted evidence | Proposed treatment |
|---|---|---|---|
| 236638 / 14715 / Sheet1 row 2 | 120 GM × 50; NO_CHANGE | Both web texts say CASE 25 × 120 GM. Eric says case changed to 50; attached image shows ×50. | Record stale-source/configuration conflict: workbook implies 3,000 GM, entered tuple 6,000 GM. Preserve values pending authority; do not infer 50 from text or automatically overwrite to 25. Eric's external confirmation can be separately recorded as a reviewed fact if authorized. |
| 235812 / 14582 / row 3 | 185 GM × 16; NO_CHANGE | Web English includes `3'S CASE 16 X 185GM`; Chinese includes 三罐裝 and 原箱16 X 185GM. Eric accepts 185 × 16. Attachment link label says `16 X 3PCS`; image shows three cans and ×16. | Preserve Eric's case-specific accepted tuple as a business label. Ask what the 3 represents before generalizing nested-case semantics. Do not silently multiply 3 × 16 or declare the extra count irrelevant for all cases. |
| 125773 / 14233 / rows 4,10 | 6 EA × 1; proposes K=1, M stays 1 | Independent Chinese item/web descriptions say 彩色長蠟燭6枝. Eric confirms six candles per pack and accepts 6 EA × 1. | Retain 6 EA × 1. Legacy 1 PK is one package, not proof of one candle. Add explicit contents-count interpretation and EA total support. Duplicate reply is one case. |
| 006270 / 15302 / row 5 | 50 EA × 1; proposes K=1, M stays 1 | Item `SACHET 50'S`, web `Sugar Satchets 50 Pieces`, Chinese 健康糖50包裝. Eric accepts 50 EA × 1. | Retain tuple. Do not change M to 50 while retaining K=50, or K to 1 while retaining M=1. Approve product-family semantics before applying this representation to all sachets. |
| 550624 / 17360 / row 6 | 550 GM × 1; proposes K=55, M stays 1 | Legacy I/J is 55 GM; independent English item/web text has `NDL\10`; Eric prefers 55 GM × 10 consumption packs. Original total corroborates 55 × 10 = 550. | Joint candidate 55 GM × 10, with count evidence from text and size from legacy. Register approved interpretation of the backslash count for this pattern; do not count B/C's 55 GM again as independent evidence. Attachment image shows 10, but link label says 5×55G: exclude both from runtime inference and record attachment inconsistency. |
| 587501 / 17380 / row 7 | 70 GM × 5; proposes K=350, M stays 5 | Legacy 350 GM exactly equals 70 × 5. Independent descriptions contain no size/count. Eric says portion information comes from image (which shows 70g×5). | Preserve existing split as total-consistent. Do not propose 350 × 5. Mark portion structure as externally confirmed by reviewer, unavailable to workbook-only inference; blank-input reconstruction must abstain. |
| 389064 / 17557 / row 8 | 116 GM × 4; proposes K=464, M stays 4 | Legacy 464 GM equals 116 × 4. Independent descriptions contain no size/count. Image lists serving size 116g and four servings. | Same preservation/availability policy as 587501. Do not infer four servings from product identity or category. |
| 292979 / 17753 / row 9 | 1 EA × 6; proposes K=6, M stays 6 | Legacy 6 PC and bilingual web descriptions explicitly say six cups/value box (6杯優惠裝). Eric accepts one cup as consumption unit. | Retain 1 EA × 6 and explain total six cups. Exact preferred decomposition is business-confirmed; independently being sold singly is reviewer knowledge, not proven by the source text alone. |
| 374470 / 17995 / row 11 | 120 GM × 1; proposes 107 GM × 1 | Legacy I/J =107 GM. Six independent descriptions contain no size. 107 appears in derived B/C and external image/link. Eric confirms 107. | Record reviewer-confirmed 107 GM × 1 for this item. The agent cannot claim it read 107 from an allowed description. With only original source inputs this remains a legacy/entered conflict requiring authority. |
| 016170 / 18091 / row 12 | 63.4 GM × 6; proposes K=380, M stays 6 | Legacy 380 GM; total entered is 380.4 GM. Eric calculates 380÷6≈63.3; independent descriptions contain neither six nor portion size. Embedded nutrition image explicitly prints 63.4g per serving, six servings, net weight 380g. | Never propose 380 × 6. Preserve pending portion-rounding policy; distinguish printed 63.4 from calculated 63.333… and the proposed 63.3. Image-only serving count remains unavailable in the runtime evidence boundary. |

The preserved external confirmations above are audit labels, not permission to fetch websites
or silently use image-derived values during a normal workbook-only run.

## 4. Quantified defect: unsafe single-field proposals

Read-only scan of all 206 SIGNIFICANT_LEGACY_SIZE_MISMATCH rows:

- 154 have existing M > 1.
- In 144 of these, proposed K equals existing K × M exactly; M is not changed.
- One further row is within one unit of that total: 016170 (380.4 versus 380).
- Nine have other differences and need individual interpretation.

Examples of what accepting the current proposal would do:

| Item | Existing total | Current proposed tuple | Resulting total |
|---|---|---|---|
| 587501 | 70 GM × 5 = 350 GM | 350 GM × 5 | 1,750 GM |
| 389064 | 116 GM × 4 = 464 GM | 464 GM × 4 | 1,856 GM |
| 292979 | 1 EA × 6 = 6 EA | 6 EA × 6 | 36 EA |
| 016170 | 63.4 GM × 6 = 380.4 GM | 380 GM × 6 | 2,280 GM |
| 550624 | 550 GM × 1 = 550 GM | 55 GM × 1 | 55 GM |

These proposals are pending, and current export preserves unapproved review rows.
This audit establishes a proposal/approval hazard, not that these totals were already
written into the baseline export. Nor does total equality prove all 144 splits correct.

Reproduction: select baseline job_items having the mismatch finding; parse original K/M
and proposed K as Decimal; count M>1 and compare proposed K to original K×M. Counts are
distinct rows, unlike finding-occurrence counts (e.g. four bilingual count findings span
three rows). Do not use the earlier benchmark's 148 whole-pack test cases as this cohort:
the benchmark and actual proposal population are different.

## 5. Current code gaps and proposed ownership

| Area | Observed gap | Required change |
|---|---|---|
| `group_a_validator.py::_legacy_issues` | Compares legacy conversion with K without receiving M or packaging role. | Compare unit, total and dimensional meaning jointly; distinguish a total-consistent representation from a genuine conflict. |
| `group_a_validator.py::_description_warnings` | Returns early for EA; GM/ML acceptance considers K or K×M but does not establish every count's role. | Add count-domain evidence and tuple checks without accepting unrelated counts. |
| `pack_size_service.py::assess` | Positive integer M returns before semantic validation. | Preserve structurally valid M but still reconcile it against reliable packaging evidence. |
| `processor.py` | A rows receive no semantic agent analysis; B pack-only requests cannot propose dependent K changes. | Queue targeted A/B packaging interpretation; reuse one semantic result per row. Keep blind extraction isolated from entered answers. |
| `provider.py` and prompt v3 | Roles exist, but output selects one size/count and forbids arithmetic; pack-only cannot express dependent changes. | Add structured packaging relations and consumption-unit basis. LLM interprets; deterministic code calculates the tuple. |
| `provider.py::validate_evidence` | Literal-substring check does not validate measurement numeric/unit correspondence; pack check expects Arabic digits despite prompt examples such as 孖裝/三件裝. | Parse and verify measurement/count observations, including approved Chinese numerals and count words, with raw fragment retained. |
| `packaging_expression_service.py` | CASE parser requires a slash; current source `CASE 25 X 120GM` misses that path. Candidate formulas encode only a few shapes. | Parse hierarchy with and without slash, preserve all counts and their levels; reject unsupported arithmetic. Use central unit registry rather than divergent factor tables. |
| `result_ledger_service.py` | Material legacy mismatch creates K proposal alone. | Produce one candidate tuple with dependent fields, totals, provenance and applicability. |
| `api/review.py` | Overrides validate individual positive fields; approval bookkeeping and export use different field/overall decisions. | Validate and approve the effective tuple atomically; partial edits trigger recomputation and revalidation. Require finite K and positive integral M. |
| `export_service.py` | Writes proposals/overrides field by field, without total/semantic validation. | Consume the exact validated final tuple used by the ledger; block unsafe mutation while allowing unchanged-row export. |
| `quality_service.py`, AI reading/evaluation services | Size/pack scoring can ignore tuple semantics or unavailable evidence. Hard-case harness omits full production review/guard behavior. | Score extraction, representation, total correctness, evidence availability and final application separately using shared resolver logic. |

Current generic `PK/PACK → EA` conversions remain valid code-managed aliases, but do not
prove a contents count of one. The resolver must preserve that source packaging meaning
for comparison; unit normalization alone cannot settle the number of consumption units.

## 6. Proposed design

```text
Original permitted text ──► deterministic extraction ──► semantic interpretation when needed
                                                               │
                                                               ▼
                                             observations + packaging relations + evidence
                                                               │
Legacy I/J + existing K/L/M + approved business policy ──────────┤
                                                               ▼
                                             deterministic joint K/L/M resolver
                                                               │
                             ┌─────────────────────────────────┼──────────────────────┐
                             ▼                                 ▼                      ▼
                       preserve/annotate                 safe joint change       human review
                             └─────────────────────────────────┼──────────────────────┘
                                                               ▼
                                              shared final tuple → UI and export
```

### Evidence model

Store field name, literal span, parsed quantity/unit, language, role, source availability,
and origin for every observation. Origins distinguish original workbook text, legacy,
existing standardized values, reviewer decision and external-only attachment information.
Derived B/C must never become evidence, even when they contain the desired answer.

Use product hierarchy as context, never as proof of a count. Equivalent English/Chinese
wording can clarify a role, but translated copies are not two independent measurements.
Maintain existing bilingual conflict detection; combining sources for packaging interpretation
does not silently reinstate the previously excluded blanket item-vs-web discrepancy sweep.

### Agent contract and prompt v4 proposal

Keep one ADK agent with an additional packaging-interpretation task initially; multiple
agents are not necessary to express this reasoning. Add:

- Product/consumption-unit description, such as cup, individual noodle portion, or contents bundle.
- All observed quantities with roles: per-piece amount, total, contents count, consumption
  count, inner-pack count, outer-case count, serving count, capacity, or unclear.
- Relations linking observations: contains, units-per-pack, amount-per-unit, stated-total.
- Competing interpretations and one short evidence-based explanation when unresolved.
- Distinct outcomes for missing evidence, ambiguous hierarchy, conflicting evidence and
  reviewer-only confirmation. Do not collapse all four into “nothing found”.

The model must ground every numeric observation. It may explain language and propose
relations, but cannot invent a missing count, browse, translate product identity into
typical size, or assert package freshness. Calculated 55×10 and 380÷6 belong in code.

For source-only inference and blind tests, keep existing K/L/M hidden. Let deterministic
reconciliation receive existing and legacy values after extraction. If a future assisted
review task sees K/L/M, identify it as a separate task and never report its result as blind accuracy.

### Joint resolver

Proposed new `services/klm_reconciliation_service.py` returns a candidate with:

- Original and proposed complete K/L/M tuples (including unchanged dependencies).
- Original, evidenced and proposed totals with dimension.
- Consumption-unit basis and packaging hierarchy.
- Formula/derivation, source references, exact Decimal result and displayed precision.
- Total comparison: exact, approved rounding difference, unsupported change, or unknown.
- Representation comparison: supported, equivalent total but unverified split, conflict, unknown.
- Policy version, candidate ID/hash, review requirement and concise human reason.

Decision examples:

1. Legacy 350 GM; entered 70 GM×5; no textual portion count: preserve, report total consistency
   and unverified split. Do not infer new values if the entered tuple is blank.
2. Legacy 55 GM; text \10; entered total 550 GM: under approved consumption policy, propose
   55 GM×10 as one candidate. Record that total is preserved.
3. Text 25×120GM; entered 120GM×50: conflict, total change unsupported; retain pending review.
4. Text six candles; entered 6EA×1; legacy1PK: retain the business-confirmed contents representation.
5. True corrected total, e.g. reviewer-confirmed107GM instead of120GM: permit change with
   reviewer provenance. Conservation is not a universal rule.

Keep structural group A/B/C distinct from semantic outcome. Do not hide a review need
because the row passes Group A numeric validation, or force old A counts to remain fixed.

### Rounding

Keep current B conversion rounding and exact preservation of existing A values unless
business explicitly changes them. Introduce a separate portion-derivation policy, initially
review-only when division is inexact:

- 380÷6 = 63.333…; one decimal yields63.3, with recomposed total379.8.
- Printed63.4×6 =380.4; that is different evidence, not the same rounding operation.
- Whole rounding63×6 =378 must not happen by accidentally reusing B conversion policy.

Store exact/printed/displayed values and residual separately. The existing imperial
conversion tolerance is not automatically a serving-size tolerance. Determine acceptable
precision and precedence of printed serving size versus calculated average with business.

## 7. Calls, performance and persistence

- Run deterministic tuple checks over all live rows first; queue only unresolved semantic cases.
- A structurally valid row with packaging clues or a total/legacy conflict can now be an AI candidate.
- Combine measurement and packaging interpretation in one response for a candidate; avoid
  independent size and pack calls producing inconsistent answers.
- Retain bounded concurrency (default five). Use a shared provider-wide limit if processing
  jobs and evaluation can run concurrently; separate per-job semaphores do not cap global load.
- Cache semantic results by permitted input hash, context, prompt/model/schema version.
  Resolve again without another paid call when only business policy or entered K/L/M changes.
- Persist outcomes incrementally for restart/resume and record request attempts, tokens,
  invalid responses, abstentions and cache hits separately.
- Produce a candidate-count dry run before live execution. Do not promise a new call count
  or latency improvement: broadening interpretation to A means the prior 71 is not the target.
- Keep existing stored runs immutable. Run the proposed engine as a new version/job or
  shadow result and compare outcomes before enabling writes.

## 8. Review, frontend and export

After backend contracts/tests, update row details with one compact comparison:

| Representation | Unit size | Pack count | Total |
|---|---|---|---|
| Uploaded | 550 GM | 1 | 550 GM |
| Proposed | 55 GM | 10 | 550 GM |

Then show: “10 noodle packs of 55 GM; total unchanged”, the exact source text, and
evidence availability. Missing independent evidence should be visible as “Total agrees;
portion split cannot be checked from supplied descriptions”. External confirmation is
labelled as reviewer information, not text discovered by the agent.

- Approve/keep/override a complete candidate. Editing one field previews the effect on total.
- Candidate hash/version prevents approving a stale proposal after a dependent field changes.
- Distinguish pending review from reviewed resolution and final download values.
- Preserve existing chat flow and optional download; unresolved rows retain original values.
- Extend audit columns with before/after total, consumption basis, derivation, evidence origin,
  policy version, resolution and short reason. Use the same final-value resolver as the UI.
- Export cache keys need both exporter version and decision/result revision.
- No live website retrieval or image inference is added by this plan.

## 9. Test and evaluation plan

First freeze the ten cases with original permitted inputs, legacy/entered values, complete
feedback, source availability and expected review behavior. Keep feedback and external
answers outside inference requests. Store small sanitized fixtures, not credentials or
the full customer workbook in the public repository.

| Test family | Required cases and acceptance |
|---|---|
| Existing total preservation | 587501,389064,292979: no suggested K-only multiplication of totals. Test all144 detected rows in a baseline replay; never auto-label their splits correct solely from equality. |
| Linked representation change | 550624:55GM×10 jointly; approval cannot produce55GM×1. Missing \10 clue must remove support for deriving M. |
| EA semantics | 125773 and006270 retain6EA×1 and50EA×1;292979 retains1EA×6. Same count with insufficient packaging context remains ambiguous. |
| External-only information | 236638 cannot learn50 from text25;587501/389064/016170 cannot reconstruct hidden serving counts.374470 cannot cite107 from forbidden B/C. |
| Nested cases | CASE25X120GM without slash;3'S CASE16X185GM;old5 CASE/10X90GM. Keep all numeric roles, expose ambiguity, do not multiply by pattern alone. |
| Local-language evidence | 6枝,50包裝,6杯優惠裝,三罐裝,孖裝. Numeric evidence validation accepts approved text numerals without permitting invented counts. |
| Rounding | Exact divisions; repeating380÷6; printed63.4; residual0.4; no reuse of integer conversion policy for servings. |
| Genuine correction | Approved107GM×1 may change120GM total with explicit authority; conservation must not freeze wrong data. |
| Review lifecycle | Atomic approval, rejection, partial override recomputation, stale candidate rejection, repeated decisions, integer M, finite values, dimension mismatch. |
| Export parity | Ledger/API/export same final tuple; pending retained; approved joint write; export regenerated after review; B/C and other source cells preserved. |
| Leakage/regression | Changing B/C never changes outcome; blind expected KLM never in request; prompt examples excluded from held-out quality claims; previous capacity/grade safety cases remain. |
| Failures | Timeout, invalid evidence, concurrent runs and partial failure leave original tuple safe; retry counts and token accounting reproducible. |

Report metrics separately:

1. Extraction/semantic-role accuracy on inputs that actually contain the evidence.
2. K/L/M representation correctness against business-confirmed labels.
3. Total/dimension correctness and unsupported total-change count.
4. Review precision and missed-conflict rate (especially rows previously marked NO_CHANGE).
5. Correct abstention for unavailable evidence; do not score website-only truths as attainable text answers.
6. Final workbook correctness after the actual production guards and approval policy.
7. Coverage, unresolved business decisions, calls, tokens, latency and repeatability.

The ten reviewed items are a targeted regression set, not a representative accuracy sample.
Add independently labelled held-out cases per pattern and run repeated live evaluations
before claiming improved accuracy. Do not carry forward the old98.8% figure after changing
what a correct representation means. Existing A is a source of entered values, not ground truth.

## 10. Delivery order and decisions still needed

1. **Evidence baseline:** freeze cases and current outputs, version labels and availability.
2. **First backend fix:** joint total-aware legacy comparison and suppression of unsafe K-only
   proposals; implement tests/replay of144 cases before adding more AI calls.
3. **Backend semantic model:** observations, hierarchy and joint resolver; all mutation paths
   use the same tuple validator. Keep unapproved generalizations review-only.
4. **Agent v4 experiment:** targeted A/B/C interpretation, evaluated against v3 with original
   allowed inputs, labelled cases and a held-out set; no silent model upgrade.
5. **Backend integration/export:** review transitions, concurrency, final-value parity,
   versioned regeneration and baseline report. Revise hardcoded EXPECTED_V02 classification
   counts deliberately if new semantic routing changes them; retain scope/purge accounting.
6. **Frontend:** only after backend contracts pass, show totals, consumption basis and
   source-aware explanations; update performance denominators and sample checks.
7. **Controlled real-data replay:** record changed tuples, retained rows, new/reduced reviews,
   wrong corrections, missed conflicts, runtime and cost before promotion.

Business decisions to resolve through concrete examples, without blocking the arithmetic safety fix:

- Which product families use contents-in-K (candles/sweetener) versus one-consumption-unit
  with count-in-M (cup noodles)? Eric's accepted examples are case-specific evidence; the
  universal boundary is not yet specified.
- What does3'S denote relative to CASE16 for235812, and why is185GM×16 the accepted total?
- For016170, should the printed63.4 or calculated63.3 be retained, and what portion precision
  and residual tolerance apply? Does a nutrition serving always define a consumption unit?
- When sources are stale or disagree, who can confirm source authority and the applicable
  product configuration/version? The agent cannot determine recency from wording alone.
- Can confirmed per-item reviewer facts be applied in subsequent runs, and how are they
  invalidated when the product/source text changes? Do not auto-learn blanket rules from approvals.

## 11. Completion criteria

No unsafe independent K/M application; all reviewed examples have a correct source-aware
explanation; total consistency and semantic confirmation are distinct; unavailable evidence
leads to preservation/abstention; backend tests precede UI work; final Excel matches the
reviewed tuple; new accuracy figures state their eligible population and unresolved cases.

This document records the investigation and plan only. The source workbook, MongoDB
records, existing decisions, prompts and processing code were not modified by this audit.
