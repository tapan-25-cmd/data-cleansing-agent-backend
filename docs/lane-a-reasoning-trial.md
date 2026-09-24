# Lane A reasoning trial

Date: 23 September 2026. Status: small trial run; full plan below, not yet built beyond the trial.

## Why

Eric's answers came from reading the descriptions and understanding the product, then
reconciling the legacy value and Excel with what he read. The deterministic engine now covers the
arithmetic part of that (whole pack, rounding, description confirms Excel, linked size × count).
This trial asks whether a second, separate AI task can reason the same way over every source
and close cases the rules cannot, without touching anything until it has proved it.

## The task

`RECONCILE` (`app/agents/reconcile.py`, prompt `uom_reconcile_v1.md`). Unlike the blind reader,
this call sees everything for one row: the six descriptions, the category, the legacy value,
the Excel values and what the rules concluded. It returns what the product's unit is, what each
source's number means, a verdict (Excel right, legacy right, description right, combined, cannot
tell), a complete proposed unit size, unit and pack size, a two-sentence explanation, quoted
evidence (validated as literal substrings), a "needs a business rule" flag, what product-kind
knowledge it used, and a confidence derived from the evidence.

It may use what kind of thing the product is (liquid or solid, single or multipack). It may
not use typical sizes, brands or prices. Results are stored in `lane_a_trials`; nothing is
written to rows, suggestions, labels or the export.

## Small trial: 20 rows, real model

Eric's ten plus one row from each open-question category. 20 calls, 33k input tokens.

| Measure | Result |
|---|---:|
| Eric's ten: reaches his answer | 8 |
| Eric's ten: cannot tell (satay beef, text is silent, Eric read the pack) | 1 |
| Eric's ten: differs (fish fillet, text says 25, Eric knew the case became 50) | 1 |
| Agrees with the engine's final values | 8 |
| Agrees with the engine's suggestion | 2 |
| Keeps Excel against the engine's suggestion | 2 |
| Different answer from the engine | 6 |
| Cannot tell | 2 |
| Confident and wrong by Eric's standard | 1 (the fish fillet) |

What the reasoning looked like on the rows the rules could not close:

| Item | Sources | AI verdict and reason |
|---|---|---|
| 062943 vanilla extract | legacy 100 ML, Excel 100 GM | Legacy right, 100 ML × 1, LOW. "Vanilla extract is a liquid." Knowledge flagged. |
| 236042 fried dace | text CASE 12 X 227GM, Excel 227 × 36 | Combined, 227 GM × 12, HIGH, quoting both languages. |
| 163287 kernel corn | text 190GX3PK / 三件裝, Excel 190 × 1 | Combined, 190 GM × 3, HIGH. |
| 483677 sweetener | legacy 50 PC, text \1G, Excel 1 GM × 1 | Combined, 1 GM × 50, MEDIUM: size from text, count from legacy. |
| 081190 XO sauce set | legacy 220 GM, text 255G | Description right, 255 GM, HIGH. |
| 349910 rice vermicelli | text \12 and 四片裝, Excel 280 × 12 | Excel right, HIGH; explains 四片裝 as the inner pack. |
| 340802 noodle | legacy 10 PC, text \6, Excel 1 EA × 6 | Excel right, HIGH; legacy contradicts the text. |
| 044305 muffin mix | legacy 12.3 OZ = 349 GM, Excel 375 GM, text silent | Cannot tell, MEDIUM. Honest. |
| 662056 gherkins | legacy and Excel 16 OZ | Kept 16 OZ. A miss: the standard unit must be GM or ML; the prompt did not say so. |

Reading of the trial: the reasoning is the kind Eric applied. It quotes the text, separates
package from piece, explains nested packs, combines a size from one source with a count from
another, and says "cannot tell" when the text is silent. Its one disagreement with Eric is the
source-authority question, not a reading error. Its one clear miss is a missing rule about
canonical units, which is a one-line prompt fix.

## Full plan

1. Prompt v2: canonical units (GM, ML, EA only), ounce policy from the category profile, and the
   representation note. Add the twenty trial rows and Eric's ten as the task's own case file with
   expected verdicts, run as a gate exactly like the thirty hard cases for the blind reader.
2. Store per job and row (`lane_a_trials`), keyed by prompt version, with a run history and a
   free rescore, like the AI reading test.
3. Trial run on the live job: all 250 review rows, 100 note rows, 100 already-correct rows
   (about 450 calls). Score per open-question category: agrees with the engine, agrees with the
   suggestion, keeps Excel, different answer, cannot tell; wrong-and-confident against every
   reviewer-verified row; false alarms on the correct rows.
4. Surfaces: "AI reasoning (trial)" section in every row drawer; per-category counts on Open
   questions; a trial box on the Pipeline diagram after the label step; a score block on the
   performance page once labelled.
5. Promotion, per category, only on the numbers: explain only → suggest for review → apply
   automatically. First candidates if the trial holds: different kind of unit (154 rows),
   description count differs (27), same total different split (16), text contradicts conversion.
6. Two business decisions surface immediately from the trial: which source is current when
   description and Excel disagree (fish fillet), and whether the AI may use product-kind
   knowledge for the unit (vanilla extract). Both are recorded on the Open questions tab.

## Phase 1 shadow run (23 September 2026, prompt v2)

Decisions applied: the description wins over Excel; product-kind knowledge (liquid or solid) is
allowed and must be declared; standard units GM, ML, EA only; no reviewer memory.

Run `f187e7a1` on the live job: all 306 open rows (250 needs review + 56 could not determine),
100 random note rows, 100 random already-correct rows. 506 calls, 0 failures, 1.1M input tokens,
0 confident-wrong against the case file (13 case rows were in the run, all matched).

| Cohort | Agrees with engine | Agrees with suggestion | Different answer | Cannot tell |
|---|---:|---:|---:|---:|
| Open rows (306) | 95 | 29 | 38 | 143 |
| Note rows (100) | 88 | 0 | 11 | 1 |
| Already-correct rows (100) | 96 | 0 | 2 | 2 |

Open rows by category: unit-kind mismatch 154 (90 suggest, 59 cannot tell, 5 could apply);
nothing in the description 54 (all cannot tell); different size 32 (27 cannot tell); description
count differs 27 (19 could apply, 7 suggest); same total different split 16 (all suggest, business
rule flagged); AI pack confirmation 12 (11 suggest); ounce 4; others 6.

Tiers on open rows: 27 could apply automatically (high confidence, quoted evidence, no business
rule, no product knowledge, no guard), 136 suggestions, 143 cannot tell.

Findings:

- The 143 "cannot tell" are almost all rows whose descriptions state no quantity. The agent
  says so and explains what a person must check. It does not guess.
- The 27 apply candidates are dominated by case-count corrections the description states in
  both languages (`CASE 25 X 120GM` against pack 50, `190GX3` against pack 1) and by the two
  `255G` rows. Under "the description wins" these are correct by definition.
- Unit-kind rows: the agent settles many with declared product knowledge (vanilla extract, kefir
  are liquids; paprika is a solid) at medium or low confidence, so they stay suggestions.
- Two false alarms on already-correct rows: a hot-pot soup base moved from GM to ML on product
  knowledge, and a cake box read as 22 GM × 6 instead of 132 GM × 6 (which may in fact be right;
  legacy 132 GM is the box). Both were medium confidence, so neither would apply.
- Eleven note-row disagreements are of two kinds: a richer representation (63 GM × 5 instead of
  5 EA × 1, held by the total-change guard and flagged as a business rule), and a Chinese web
  description stating 720克 where legacy and Excel say 540 GM (the description wins by the rule
  we set; whether the description is stale is exactly the source question).
- Rounding: the agent rounds 1.9 OZ to 54 GM where Excel has 53; the engine's tolerance already
  treats this as a note. The agent's own rounding must follow the engine's, not the other way.

Surfaces built: `GET /jobs/{id}/reasoning` and `/reasoning/{row}`; the "AI reasoning (trial)"
section in the Results, Open questions and Past vs New drawers; per-category counts on the Open
questions table; the trial box on the Pipeline diagram. Nothing is applied.

## Recommended phase 2

1. Promote to "suggest" now: description count differs, text contradicts conversion, languages
   disagree, same total different split, AI pack confirmation. The agent's proposal becomes the
   review suggestion with its explanation.
2. Promote to "apply" after one more labelled pass: description count differs where both
   languages state the count and the unit size matches Excel (19 rows here).
3. Keep as explanation only: unit-kind mismatch (product knowledge), different size (mostly cannot
   tell), nothing in the description.
4. Prompt v3: rounding must match the engine (nearest whole, Excel style); a "richer
   representation" answer (EA → GM × N) is a business-rule flag, never a verdict against Excel.
5. Decision needed: when a description states a different size than both legacy and Excel
   (720克 vs 540 GM), is the description current? Three rows here; the rule says yes.

## Making it production grade

Where it is shown today: the Pipeline diagram (its own box after the label step, with the three
outcome tiers), the Rules tab (sub-tab "AI reasoning (trial)": what it sees, never sees, returns,
its fixed rules, the outcome policy, safety checks, how it is tested, the latest run), the Agent
performance page (section 7, run summary per cohort), the Open questions table (per-category
counts) and every row drawer (the full answer for the row).

What still separates it from production, in the order to close the gaps:

1. **Same rounding and units as the engine.** Convert and round inside code, not in the model:
   the model returns the source value and unit it chose; the rule engine converts. Removes the
   53 versus 54 GM class of differences and the risk of an invented factor.
2. **Deterministic policy in code.** The tier decision (apply, suggest, stays) lives in the
   trial service today as scoring. Move it into the result ledger as a versioned policy with
   its own finding codes (`REASONING_APPLIED`, `REASONING_SUGGESTED`, `REASONING_CANNOT_TELL`),
   so a row's label, comment and export come from one place, and Past vs New shows "closed by
   reasoning" as a change category.
3. **Run it inside the job**, after the ledger and before the review stage: bounded concurrency
   shared with the reader, retries with backoff, per-row persistence so an interrupted run
   resumes, token and latency accounting on the job, and the same progress reporter. Export and
   download never trigger it.
4. **Idempotent and cacheable.** Key each answer by the row's permitted text, category kind,
   legacy, Excel values and prompt version. A re-run with unchanged inputs reuses the stored
   answer at no cost; a changed prompt version invalidates it.
5. **Gate that grows.** The twenty-case file becomes the release gate for the reasoning prompt,
   run from the performance page like the thirty hard cases. Every reviewer decision on a row
   with a reasoning answer is added as a labelled case (verdict and values), so the gate learns
   from real approvals and rejections without any auto-learning of rules.
6. **Promotion per problem category, with numbers.** A category moves from shadow to suggest
   when its shadow answers agree with reviewer decisions at or above the agreed threshold and
   have zero confident wrong answers; from suggest to apply only for the "could apply" tier and
   only after a second labelled pass. Each promotion is a versioned policy entry with the date
   and the evidence, visible on the Rules tab.
7. **Observability.** Per-run counts of verdicts, tiers, guard hits, invalid responses and
   repairs, average latency and tokens, and a drift check: if the share of "cannot tell" or of
   guard hits moves by more than an agreed margin between runs on the same workbook, the run is
   flagged before anything is applied.
8. **Cost control.** Only open rows are reasoned in production (about 300 per full run); the
   note and correct samples are trial-only. A per-run token budget stops the pass and marks the
   remaining rows "not reasoned" rather than failing the job.
9. **Failure modes handled.** Timeout, invalid answer after repair, evidence not found, or a
   guard hit each leave the row exactly as the rules left it, with a finding that says the
   reasoning did not apply and why.
10. **Prompt hygiene.** Worked examples that are also in the gate are excluded from any
    accuracy claim; the prompt carries a version and checksum stored on every answer.
11. **Business decisions recorded, not inferred.** The description-wins rule, product-kind
    knowledge and the representation choice are entries in the business decisions file with a
    date, shown on the Rules tab, and referenced by the prompt version that applies them.

Estimated effort: two to three days for 1 to 4 and 9, one day for 5 to 7, half a day each for
8, 10 and 11, plus one labelled pass with the reviewer for the first promotions.
