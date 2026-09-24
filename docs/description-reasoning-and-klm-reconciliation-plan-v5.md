# Description reasoning and joint K/L/M reconciliation — implementation plan v5

Date: 23 September 2026  
Status: implementation baseline

This plan supersedes the implementation portions of `eric-review-klm-reconciliation-plan-v4.md`.
The v4 document remains the immutable evidence audit for Eric's workbook and the September
baseline run. This document defines the next engine, its safety boundary, its evaluation,
and the new Past vs New product experience.

## 1. Objective and measured baseline

The next engine must understand what a product description means, not merely copy the first
number it finds. It must interpret the permitted English/local-language description pairs,
identify what every relevant quantity describes, and reconcile the complete K/L/M tuple:

`standardized unit size K × standardized pack size M = total sellable/consumption quantity`,
expressed in standardized UOM L.

Pinned comparison baseline: job `c658c48e-2cd9-46a4-a981-42780e3120dd`.

| Baseline outcome | Rows |
|---|---:|
| Selected | 13,300 |
| Purged | 758 |
| Live | 12,542 |
| No change | 11,454 |
| Automatically applied | 492 |
| Observation only | 102 |
| Review required | 438 |
| Unresolved | 56 |

Population screening found 144 review proposals where proposed K equals existing `K × M`
while M remains unchanged. It also found 1,417 non-review rows with count evidence or multiple
measurements, and 15 non-review rows where an explicit count-size expression disagrees with M.
These are investigation candidates, not confirmed errors; cohorts overlap.

## 2. Evidence boundary and pair semantics

Excel columns B/C remain excluded because they are derived concatenations. Runtime evidence is:

- item-description pair: English plus local language;
- web-description pair: English plus local language;
- brand text as product context only, never standalone quantity or pack evidence;
- division/category/subcategory/section as context only;
- legacy I/J and existing K/L/M only in deterministic reconciliation, after blind interpretation.

Each description pair is interpreted independently. Within one pair, English and local-language
text may complement one another. If one member clearly states a quantity and the other is silent,
the pair supports that quantity. Silence is not conflict. A genuine contradiction within the pair
is recorded as a pair discrepancy and neither side is silently selected.

One sufficiently clear pair may support a result even if the other pair has less detail. The
engine does not create a blanket item-versus-web discrepancy. If two independently valid pairs
support incompatible final quantities, the resolver records competing supported interpretations
and preserves the uploaded tuple pending an approved source-authority rule or human decision.

A pair may support only part of K/L/M. For example, “four cans” supports a count but does not
support the volume of each can.

## 3. Interpretation is different from extraction

The interpreter answers four questions:

1. What product or consumption unit is described?
2. What does every quantity refer to: per-unit contents, total contents, contents count, inner
   pack, outer case, serving, capacity, dimension, name/grade, or unclear?
3. How are quantities related: contains, amount-per-unit, units-per-inner, inners-per-case,
   stated total, or alternative interpretation?
4. What is explicitly supported, contradicted, missing, or ambiguous in each bilingual pair?

The agent returns structured observations, relationships, alternatives, evidence fragments and
a short user-facing explanation. It does not calculate final K/L/M, invent missing values, use
typical product sizes as facts, browse websites, or expose hidden chain-of-thought.

Deterministic code performs conversions, multiplication/division, precision handling, tuple
validation, source comparison and the final decision.

## 4. Agent contract v4

Add task `PRODUCT_INTERPRETATION`, retaining legacy tasks during migration. Its response contains:

- one result per description pair: `SUPPORTED`, `INSUFFICIENT`, `CONFLICT`, or `AMBIGUOUS`;
- product/consumption-unit description;
- all grounded quantity observations with source field, literal fragment, value, unit and role;
- packaging relations between observations;
- zero or more supported K/L/M representations;
- competing interpretations and missing facts;
- concise evidence-based explanation.

Evidence validation must verify that fragments occur in permitted fields and that parsed numeric
claims match those fragments, including approved Chinese numerals/classifiers. Brand values and
category context cannot become quantity evidence.

The first implementation keeps one ADK interpreter. A second critic is not added until evaluation
shows a measurable failure mode it fixes. Identical source interpretation can be cached by an input
hash containing the permitted text, context, prompt, model, schema and interpreter version. Every
row is still reconciled separately against its own legacy and K/L/M values.

## 5. Deterministic joint resolver

Add a single `KLMReconciliationService` used by processing, review, ledger, export and comparison.
It accepts source interpretations, legacy I/J, existing K/L/M, conversion rules and versioned
business policies. It returns:

- complete uploaded and candidate tuples;
- uploaded and candidate totals and dimensions;
- the role assigned to legacy and described quantities;
- exact formula, precision/residual and evidence references;
- result: preserve, safe automatic correction, review, or insufficient information;
- reason code and short human explanation;
- candidate hash and all contributing engine versions.

Required safety rules:

- compare per-unit with per-unit and total with total;
- never replace K with an apparent total while retaining M > 1;
- if legacy equals existing `K × M`, treat it as total-consistent evidence, not a standalone K
  correction;
- do not infer a missing pack split from arithmetic alone;
- allow a supported total to change when authoritative evidence proves the old total wrong;
- validate K as finite and positive, L as canonical and dimensionally compatible, and M as a
  positive whole number;
- approve or override a complete candidate atomically, then recompute it;
- export only the tuple validated by this shared resolver.

## 6. Business policy versus language understanding

The interpreter determines what the supplied descriptions mean whenever they contain enough
information. Policy is needed only where the description cannot determine the organization's
preferred representation or source authority, for example whether six candles are stored as
`6 EA × 1` or `1 EA × 6` when both total six.

Create a versioned packaging/consumption policy for representation choices, trusted source roles,
precision and approved notation families. Policies are small and semantic; do not create a rule
for every sentence. Reviewer decisions may become item-specific facts only when explicitly
approved, versioned and invalidated when relevant source text changes.

## 7. Processing and call strategy

All live rows receive deterministic structural and semantic screening. Candidate interpretation
includes current reviews, unresolved rows, structured packaging clues, count/measurement conflicts,
and a representative sample of currently clean rows for missed-problem measurement.

Do not equate candidates with paid calls. Resolve deterministic cases first, reuse cached source
interpretations, then call the agent only where meaning is unresolved. Use bounded provider-wide
concurrency, finite retries, incremental persistence and resume after interruption. Agent failure,
no evidence and ambiguity remain distinct outcomes. Export/download never triggers inference.

## 8. Group behavior

- Group A: structural validity is followed by semantic consistency screening. Suspected issues
  receive description reasoning and joint reconciliation. A valid format is not automatically
  independent semantic confirmation.
- Group B: deterministic conversion remains primary; the resulting tuple is then checked at the
  correct packaging level. Nearest-whole conversion applies only to the conversion it owns.
- Group C: interpretation derives only supported values and relationships. Missing size with a
  known count remains partially known rather than guessed.

Group remains the processing route. User outcomes remain separate: no change, corrected safely,
note only, human review, could not determine, or purged.

## 9. Pair-level discrepancy behavior

For each item/web pair:

| Pair evidence | Result |
|---|---|
| One language states a fact; the other is silent | Use the fact |
| Both languages support the same meaning | Use the combined pair evidence |
| Both contain compatible detail at different specificity | Use the richer supported meaning |
| They contradict on the same quantity role | Pair discrepancy; do not pick a winner |
| Pair supports count but not unit size | Preserve partial evidence and unknown size |

Across item and web pairs, compatible findings may strengthen explainability. A missing value in
one pair does not weaken a clear value in the other. Incompatible pair conclusions become competing
interpretations, not a reintroduction of the retired blanket cross-source discrepancy detector.

## 10. Past vs New tab

Add a third tab beside Results ledger and Agent performance: **Past vs New**. It compares two
real runs of the same workbook, row by row: the run this job was created to compare against
(or, for an ordinary upload, the most recent earlier finished run of the same file) and this run.
It never invents a "shadow" answer and never writes to either run.

The page reads top to bottom:

- Which two runs are being compared, when each ran, and how many rows changed.
- Badges for what changed, in plain words: same result, no longer needs review, now corrected
  automatically, different final values, different suggestion, now needs review, now could not
  determine, note added or removed. Clicking a badge filters the table.
- A small table of how many rows carry each result in the past run and in the new run.
- The reviewer-confirmed answers (`app/evaluations/reviewer_confirmed_cases.v1.yaml`): for each of
  Eric's ten products, the answer he accepts, and whether each run reaches it. A run "matches"
  when the workbook would carry the reviewer's value and no contrary suggestion is pending;
  "suggested" when the reviewer's value is proposed and waits for approval; "wrong suggestion
  pending" when the right value is kept but a different suggestion still waits for review.
- The row table: product, what the past run said (status, values, any suggestion), what the new
  run says, and what changed with a one-sentence explanation. Every status and comment uses the
  same wording as the results ledger and the exported workbook.

Backend: `GET /jobs/{id}/comparison` builds the comparison once in the background (reading two
13k-row jobs from Atlas takes minutes), stores the summary in `job_comparisons` and one document
per row in `job_comparison_rows`, answers 202 while building, and then pages and filters from
storage. A stored comparison is rebuilt when either run changes (a review decision updates it).

## 11. Evaluation and historical-data replay

The ten Eric cases are targeted regressions, not a representative accuracy set. Build independent
business-labelled cases covering unit/total, EA representation, bilingual complement/conflict,
nested packs, capacity, serving information, rounding, missing information and already-correct rows.

Report separately:

- interpretation role and relationship accuracy;
- complete K/L/M correctness;
- total/dimension correctness;
- automatic-change precision;
- preservation of correct existing values;
- discrepancy detection and missed-conflict rate;
- useful-review precision;
- safe abstention;
- calls, tokens, latency, retries, cache hits and repeatability.

Historical replay order:

1. Freeze the 13,300-row baseline and result revisions.
2. Run deterministic v5 shadow reconciliation over all live rows.
3. Run interpretation only for the candidate queue; record availability and cost before calls.
4. Compare old/new outcomes without applying them.
5. Label stratified samples from changed, unchanged and unresolved cohorts.
6. Publish accuracy only for labelled eligible denominators.
7. Promote automatic behavior one proven case family at a time.

## 12. Delivery sequence and acceptance

1. Total-aware Group A legacy comparison and regression protection for the 144 unsafe patterns.
2. Joint tuple models/resolver and atomic tuple validation.
3. Pair-aware interpretation contract, prompt and evidence validation.
4. Shadow comparison service/API and deterministic historical replay.
5. Agent-backed targeted shadow run and held-out evaluation.
6. Past vs New frontend tab.
7. Shared review/export final-value integration after backend parity tests pass.

Acceptance requires: no independent K/M proposal can create an unsupported multiplied total;
pair silence is not treated as conflict; all numeric interpretations cite permitted evidence;
Group A semantic issues are screened; shadow results never mutate production decisions; the UI
does not claim accuracy without business labels; ledger, review and export resolve the same tuple.

## 13. Implemented and measured (23 September 2026)

Implemented:

- `KLMReconciliationService` classifies legacy evidence as unit, whole-pack total,
  rounding-only, dimensional conflict, uncomparable, or material mismatch.
- Group A validation uses the complete K/L/M tuple before creating a legacy proposal. Whole-pack
  consistency produces `LEGACY_TOTAL_CONSISTENT` (a note) and never the old K-only proposal.
- Review approval blocks unsafe K-only whole-pack proposals still present in historical jobs;
  M overrides must be positive, finite whole numbers.
- Agent prompt v4 adds independent bilingual-pair conclusions and grounded quantity
  relationships; the provider retries once with the validation error on malformed output.
- Past vs New tab, comparison service and stored background build (section 10).
- Reviewer-confirmed cases file with Eric's ten answers, scored for both runs.

Live full-pipeline runs (four runs of the pinned 13,300-row workbook with the real agent, all
tagged `smoke_metadata.purpose = v5-real-pipeline-comparison`, baseline `c658c48e`):

| Result | Baseline | New engine |
|---|---:|---:|
| Group counts A / B / C / disputed / purged | 11,970 / 512 / 55 / 5 / 758 | identical |
| Needs your review | 438 | 293 |
| Correct, with a note | 102 | 246 |
| Corrected automatically | 492 | 493 |
| Unsafe "K becomes the whole-pack total" suggestions | 144 | 0 |
| Rows differing between successive new runs | | 0 |
| Agent calls per run | 71 | 70–71 |

145 rows changed: 144 whole-pack reviews became notes with the uploaded values kept, and one
voucher (246033, `12PCS COUPON`) is now filled as 1 EA automatically because the agent declines
the 12 as contents rather than reading it as a pack size.

Reviewer-confirmed answers reached (workbook carries the reviewer's value with nothing contrary
pending): baseline 2 of 10, new engine 5 of 10 (587501, 389064, 292979 added). Still open:
125773 and 006270 (contents-in-K representation; the engine still suggests K=1), 550624 (needs
the joint 55 GM × 10 candidate from the `\10` count), 016170 (legacy 380 vs total 380.4 is an
identity conversion, so the rounding tolerance does not apply and K=380 is still suggested),
374470 (107 GM is suggested and waits for approval; the agent cannot cite it from permitted text).

Description confirms Excel (added after the review of 125773): before the legacy check sends a
Group A row to a person, the four permitted description fields are read. If they state the value
Excel already has, the legacy value cannot be a contradiction and the row is confirmed with a note:

- `DESCRIPTION_CONFIRMS_PIECE_COUNT`: Excel says N EA × 1, the description states N pieces
  (`6枝`, `50包裝`, `4'S`, `\20`), and the legacy is one package (1 PK) or a weight/volume
  (32 GM per cube). A legacy piece count that disagrees (12 PC vs 6 EA) still goes to review.
- `DESCRIPTION_CONFIRMS_UNIT_SIZE`: the description states Excel's unit size and either also
  states the pack count (`500MLX2` for 500 ML × 2) or the legacy is one package with M = 1. A
  description that states only the size while the legacy carries a count (50 PC vs 1 GM × 1,
  text `\1G`) is not a confirmation, because the 50 may be the missing pack size.

Offline replay of the 13,300-row workbook with this rule (mock agent, so automatic counts differ
slightly from the live runs): group counts unchanged; reviews 293 → 221; 41 piece-count and 19
unit-size confirmations. Of the 293 reviews in the live run, 174 have descriptions that state no
quantity at all, and the 512 Group B automatic conversions have no description that states a
different size. Reviewer-confirmed answers reached rise from 5 to 7 of 10 (125773 and 006270 added).

Three further deterministic changes (`group-a-validation-v5`, `klm-reconciliation-v2`,
`result-ledger-v4`), built without any category policy:

- `LINKED_SIZE_AND_PACK_SUGGESTION`: when the converted legacy size × a count stated in a
  description equals Excel's total, the suggestion is size and pack together (55 GM × 10 for
  550 GM × 1 with `\10`), total unchanged, as a review. A size-only suggestion would have
  shrunk the total. Replay: 16 rows, all noodle multipacks and drink multipacks.
- Whole-pack rounding allowance also when the legacy unit already equals Excel's unit: 380 GM
  against 63.4 GM × 6 = 380.4 GM is a note. The exact rule still protects the unit comparison.
- `DESCRIPTION_PACK_COUNT_DIFFERS`: the description states Excel's unit size with a count that
  is not Excel's pack size (`CASE 25 X 120GM` against 120 GM × 50). Two sources disagree and
  nothing says which is current, so it is a review with no suggested value. Replay: 27 rows.

Replay after all changes: group counts unchanged; reviews 293 → 239 (217 plus the 27 pack-count disagreements, some overlapping); the remaining reviews are
rows whose descriptions state no usable quantity, plus the 16 linked suggestions and the AI pack
confirmations. Reviewer-confirmed answers: 7 of 10 carried with nothing contrary pending (016170 added,
236638 now a review because its description disagrees with Excel), 2 correct suggestions awaiting approval (550624 as 55 GM × 10, 374470 as 107 GM).

Live run with all of the above (job `a17e093a`, 23 September 2026, 71 agent calls, 144 s), compared
with the pinned baseline on the Past vs New page:

| Outcome | Baseline | New engine |
|---|---:|---:|
| Needs your review | 438 | 250 |
| Correct, with a note | 102 | 310 |
| Corrected automatically | 492 | 494 |
| Rows changed | | 248 (208 reviews cleared, 16 linked suggestions, 22 new reviews, 2 now automatic) |

Reviewer-confirmed answers on the live run: 7 carried and confirmed, 1 carried but under review
(236638, description disagrees with Excel), 2 correct suggestions awaiting approval (550624, 374470),
0 contrary suggestions pending. Group counts unchanged.

Hard-case gate (30 cases, `agent_eval_cases.v1.yaml`): prompt v3 passed 30/30. Prompt v4.1
passed 25/30 with 4 confident wrong answers (capacity `5OZ PUDDING CUP`, name/grade `3.3G
YOGHURT` and `4L VINEGAR`, joined sizes `200ML+15ML`) and one failed call (`8 PACK` without pack
evidence). Prompt v4.2 restored the "numbers that are not a size" rules from v3 and passed 28/30
(`VINEGAR5G` read as 5 GM; `(8 PACK)` returned as a sellable pack). Prompt v4.3 adds the
weight-on-a-liquid and contents-count-in-brackets rules and passes 30/30 with 0 confident wrong
answers, matching v3. All three runs are stored in the `agent_evaluations` collection and shown on
the performance page. v4.3 is the production prompt.

## 14. Open questions and Rules tabs (23 September 2026)

Two read-only tabs for presenting the work; neither changes a row, the export or the engine.

**Open questions** (`GET /jobs/{id}/open-questions`, `/open-questions/{category}`,
`PUT /open-questions/{category}/answer`). Every row that is not "already correct" or purged is
placed in exactly one category by `app/evaluations/open_questions.v1.yaml` (finding codes tried in
order). Six groups: legacy vs Excel, pack structure, conversion, what the AI read, notes to confirm,
changed automatically. A category shows its rule, its question, the options and every row with the
six descriptions, legacy I/J with its conversion, Excel K/L/M with total, the suggestion with total,
the Excel comment, the findings with quoted evidence, and a "this or this" block. A business answer
is recorded per category (collection `open_question_answers`) and shown on every workbook. The
live job has 1,110 rows across 21 categories.

**Rules** (`GET /rules?job_id=`). The flow in eight steps, the unit mapping table with the units
the workbook needed that are missing, the seven legacy-check outcomes in order, what the text
reader recognises (units, pack words, count notations, Chinese counters and numerals, pairs) and
what is never a size, the AI rules and safety checks with the liquid categories learned from the
workbook, decisions D1 to D6 and the open business questions, and the versions the job ran with.
Text lives in `app/evaluations/rules_guide.v1.yaml`; tables come from the code and the job.

**Pipeline** (`/jobs/{id}/pipeline`, definition under `pipeline:` in `rules_guide.v1.yaml`, served by
`GET /rules`). A lane diagram drawn in the browser: upload → select → read → purged? → which lane?
→ Lane A (legacy comparison, description comparison) / Lane B (unit table, safety checks) / Lane C
(AI reading, safety checks) → one label per row → people decide → download, with "measure and
compare" alongside. Arrows are drawn from the real node positions, each node shows the live count
from the job (rows in the file, in scope, skipped, per lane, AI calls, rows to review, the six
labels), and clicking a node opens the standard drawer with what happens, what goes in and out,
and a link to the matching Rules section.

## 15. Accuracy tab for stakeholders (24 September 2026)

`GET /jobs/{id}/accuracy` (built once per job version in the background, 202 while building) and
`/accuracy/{set}` for the products behind a set. `app/services/accuracy_service.py` places every
live product of Groups A, B and C into one named set with a plain reason. No reviewer is needed:
a result counts as confirmed when an independent witness agrees (legacy value exact, whole pack or
rounding; the description states the same values; the category's own unit pattern at 90 percent
or more; the AI reasoning layer), a flag counts as correct when the witnesses truly disagree,
products with no witness are shown as unverified and left out of the score, and the reasoning
layer's high-confidence quoted answers turn an unneeded review into an alarm or a wrong kept value
into a miss. Group C "found nothing, and nothing is written" is a confirmed correct answer.
Accuracy = (confirmed + correct flags) ÷ (products − unverified), per group, all products counted.

First build on job 9ea8ad60: Group A 99.9% (11,926 of 11,943; 32 unverified; 17 alarms from the
reasoning layer), Group B 100% of 211 scorable (301 converted rows have no witness yet: the
reasoning layer has only run on open rows), Group C 100% (55 of 55 found nothing where nothing is
written). The 370-product blind test and the 30 hard cases remain the only true answer keys and
are linked from the page.

### 15a. Cross-validation and the v2 scoring (24 September 2026)

Five independent validators (recount from first principles, row audits of A and of B/C, a
methodology critic, a code review) reproduced the v1 arithmetic exactly (23 sets, 12,542 live
rows, every row in exactly one set) and rejected the v1 definition: the legacy field is the same
entry copied for 92.5% of Group A, Group C's 100% scored the reader by its own silence, Group B's
100% hid 59% unscored rows and rows contradicted by their text, the alarm rule accepted a quoted
product name as evidence, and a piece count got a rounding allowance (1 PC ≈ 2 EA).

`accuracy-v2` answers each point:

- Kinds: CONFIRMED (the product's own text states the value), CONSISTENT (another stored record
  agrees: legacy, unit table, category pattern; labelled as consistency, not proof), FLAG, ALARM,
  WRONG, UNVERIFIED. Score = (CONFIRMED + CONSISTENT) ÷ (CONFIRMED + CONSISTENT + WRONG); flags
  beside the score, never inside; coverage always shown.
- The description is consulted before the legacy field; a same-dimension value that matches
  neither the unit size nor the whole pack counts as WRONG (description wins).
- An alarm needs quoted evidence that states Excel's value (17 → 7).
- Group B: only the text confirms a value; the category pattern only settles an ounce; respelled
  units are their own consistent set; the text can contradict (WRONG).
- Group C: a blank counts as correct only when the reasoning layer also read nothing; pack flags
  are reachable; a read value must be found in the text.
- Reconciliation: no rounding allowance for EA counts (`klm-reconciliation-v3`); the extractor
  reads a glued `EA` suffix as a count.
- API: a failed build is reported, not retried forever; the previous report stays readable
  while rebuilding; `?rebuild=true`; unknown set → 404; per-row witness sentence in drill-down.

Job 9ea8ad60 under v2: Group A 99.9% (11,693 of 11,709 scored: 483 confirmed by text, 11,210
consistent, 16 wrong; 233 to a person of which 7 not needed; 33 not checkable). Group B 100% of
111 scored, coverage 21.7% (26 text-confirmed, 85 consistent, 385 not checkable). Group C 54
correctly blank with a second reader agreeing, 1 pack flag. Known-answer anchor: 353 of 370 on
the blind reading test.

The page (judged design): one number, a five-box chain (all products − not checkable − to a
person = checked → correct = percentage), by-group rows under the chain, a "what the correct
answers rest on" bar (text versus another record), per-group cards with a mini chain, meter,
set bars grouped by outcome, a receipt that adds up to the equation, and an "other ways to count
it" ladder (flags as wrong, not-checkable as wrong, text only, known-answer test). Product lists
sit behind each set with a witness sentence per product.

### 15b. Accuracy page, final shape (24 September 2026)

Top: overall number and the sum that builds it, then three group tiles as tabs (opens on B).
Each group, in the same order: what the task was, framed on the workbook columns (I/J, K/L/M,
the six descriptions) with a worked example; one bar of what happened to every product; three
ways to read the accuracy, each a question with its number and fraction (1. did the tool do its
job on every product, 2. can a second source in the file confirm the value, with the products
that have no second source left out, 3. the strictest reading, where "no second source" is
described as a fact about the data, never as a tool failure); the products the tool did not
decide alone, grouped by reason with the products behind each group; then the plain-word steps.
Group C's second reading is the known-answer test (353 of 370). Nothing on the page calls a
value "confirmed" unless the product's own words or a second reading state it.

### 16. Round-trip (idempotence) check and reader contract hardening (24 September 2026)

Offline test: process the v0.2 workbook, export it, feed the export back in as input, process
again (`scratchpad/roundtrip.py`, mock agent). Before the fix the export re-imported with 154
rows in `DATA_SHAPE_ERROR`: Group B conversions whose pack count was never found were exported
as size + unit with an empty pack size, an incomplete tuple the reader rejects. Fix
(`result-ledger-v5`): a Group B conversion with no pack count written anywhere and no review
open is recorded as a single item, pack size 1, with the note `PACK_SIZE_SINGLE_ITEM`. After the
fix the export re-imports with 0 invalid rows and 0 automatic changes; the only rows still in
Group B are the 7 whose size stayed empty pending a person. Group counts on the original file
are unchanged (A 11,970 / B 512 / C 55 / disputed 5); 167 rows carry the new note.

Reader contract (`provider.py`): a `pair_interpretations.conclusion` longer than 400 characters
is trimmed instead of rejected, and a reason code that disagrees with the status is replaced by
the one the status implies. Both used to cost a schema-repair retry (seen as ADK tracebacks in
the server log on job 46faa97e, which still completed with 0 invalid responses).

## 17. Blind tests (24 September 2026)

Sub-tab under Accuracy (`/jobs/{id}/blind-test`, `app/services/blind_test_service.py`, store
`blind_tests`, API `GET /jobs/{id}/blind-test`, `/{kind}/rows`, `POST /{kind}/run`). The answer
exists before the tool runs and is hidden from it; nothing changes a row.

- **Group B, conversion test** (no AI): complete Group A products whose older field is in
  another unit; K/L/M hidden; the unit table's output compared with the team's value. Job
  9ea8ad60: 1,112 needed a conversion; 859 exact + 120 whole-pack = 979 reproduce the team's
  value; 100 differ (37 OZ → GM where the team used the fluid reading, 7 LB, and 53 where the
  team's unit is a different kind from the older field's); 33 use units the table does not know
  (BX, SET, RL, BG, ST, PA, CT, CP). Same unit: 10,586 of 10,630 copy across unchanged.
  Pack sizes above 1: 1,033 of 1,203 are written in the description.
- **Group A, seeded-error test** (no AI): 2,000 evenly spaced clean products, each broken in
  up to six realistic ways, run through the Group A check. 7,701 of 7,955 seeded errors caught
  (sent to a person); unit swapped 1,849/1,849; decimal shift 1,849/1,849; digit typo
  1,985/2,000; size-as-total 179/204 (25 noted only); off by one gram 1,795/1,849 (same-unit
  legacy must match exactly); pack off by one 44/204, the rest noted only: a wrong pack size is
  detectable only when the description states the count, because the older field carries the
  unit size alone. Untouched copies flagged: 5 of 2,000.
- **Group C, reading test**: positive half is the existing AI reading test (353 of 370);
  negative half (`C_SILENT`, one call per product) shows the reader 200 products whose
  descriptions state no size and expects "nothing written". Not yet run on this job.

Both free tests were run from a standalone process; runs triggered through the dev server with
`--reload` were cancelled mid-way (`_OperationCancelled`) when the worker restarted.

### 17a. Pack count of one needs no confirmation (24 September 2026)

Seven vouchers (legacy 1 PC, Chinese description ending in 1PC) were sent to a person only
because the reader had read the pack count "1" from the text and every AI-read pack count
triggered `AI_PACK_NEEDS_CONFIRMATION`; three sibling vouchers with identical text were applied
automatically because the reader happened to decline. `guards.pack_count_is_settled`: a pack
count read from the text needs no confirmation when it is 1, or when it equals the piece count
the legacy field already holds. Those seven now apply as 1 EA × 1; the two coupons where the
count could be contents (12S, 4PC) still go to a person. v0.2 group counts unchanged.
