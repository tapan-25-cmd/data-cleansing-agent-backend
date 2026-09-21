# Agent Performance — Stakeholder Quality Page

## Purpose

Show a non-technical stakeholder or client how good the data cleansing agent is **on
their own workbook**, as a product feature — not how our pipeline tests are doing. The
page uses tables, plain language, and no internal vocabulary (no groups, rule ids,
finding codes, or "K/L/M").

Route: `/jobs/:jobId/performance` · API: `GET /api/jobs/{job_id}/quality` ·
Service: `backend/app/services/quality_service.py` (`quality-report-v3`).

## Headline: accuracy of what the agent produced (agreed 21 Sep 2026, option A)

The page leads with the accuracy of the agent's **results** — every action it applied
and every suggestion it made — not with agreement against data that already existed.

| Kind | Type | Independent text check? |
|---|---|---|
| Sizes and units it converted | Action | Yes |
| Blank sizes it filled from legacy data | Action | Yes |
| Unit spellings it tidied | Action | Yes |
| Pack sizes it filled from the description | Action | No — read from that text |
| Sizes the AI read from the description | Action | No — read from that text |
| Corrections it suggested | Suggestion | Yes |
| Products it flagged for a person | Suggestion | No |
| Products it left blank | Suggestion | No |

Two witnesses, shown side by side and never mixed into one count:

1. **Product text (automatic).** Where a description states a size, does it confirm the
   result, per unit or as the whole-pack total? Only valid when the result did *not*
   come from that text; otherwise the check would be circular.
2. **Reviewer (ground truth).** A repeatable random sample per kind
   (`GET /jobs/{id}/verification-sample`, ordered by a hash of job, kind and row so it
   cannot be cherry-picked). A person marks each Correct or Wrong
   (`PATCH /jobs/{id}/items/{row}/verification`). A verdict never changes K/L/M or the
   export. Approving or replacing a suggestion in review also counts as a verdict.

The headline uses reviewer verdicts once 30 results are verified; until then it uses the
text check and says plainly how few results that covers.

**v0.2 reality:** product text can check only **20 of 995** results (18 confirmed, 90%),
because most of these products state no size in their description and a count such as
`1 EA` cannot be confirmed by text. All 7 checkable suggestions were confirmed (the text
backs the agent over Excel); the 2 misses are real catches (legacy 220 GM, product name
says `255G`). The text figure is therefore a first signal only — the trustworthy number
comes from the reviewer sample.

The blind test below is kept as supporting evidence ("how we tested the agent before
trusting it"): 95.7% across 12,451 checks, 99.5% if every open decision goes the agent's way.

## Blind test: what "correct" means (agreed 21 Sep 2026)

The workbook already contains an answer key: the products whose size, unit, and pack
size a person entered and that pass validation. The page runs a **blind test** on them:
the entered answers are hidden, the agent works each value out again from the remaining
source data, and the two are compared.

The result is reported in **two parts that are never blended**:

1. **Agreement** — the agent reached the same answer as the team. This is the headline
   figure, always shown with its sample size.
2. **Awaiting your decision** — the agent and the team differ. This is *not* counted as
   an agent error, because the entered value may be the wrong one (item `044305`:
   `12.3 OZ` is 349 GM, Excel says 375 GM). These products are listed as open business
   decisions. Once a decision is recorded they count for or against the agent.

`BlindInput` has no field for the hidden answers, and a test proves predictions do not
change when the answers change (FR-39).

## Page structure

1. **Your workbook** — where every product ended up (already correct, corrected
   automatically, needs a person, left blank, skipped). Labelled as workload, not accuracy.
2. **Accuracy** — one row per capability: products tested, same answer as your team,
   agreement %, awaiting your decision, status. Each row expands to a plain examples
   table (what the agent was given, its answer, your team's answer, in plain words).
3. **Decisions we need from you** — one row per decision (not per test case): products
   affected (live count), a real example, and what the answer unlocks. Expands to the
   reason and the options.
4. **Engineering checks** — the golden regression cases, collapsed, explicitly labelled
   as not a measure of accuracy on the client's data.

## v0.2 baseline figures

| Capability | Products tested | Same answer | Agreement | Awaiting decision |
|---|---:|---:|---:|---:|
| Converts size and unit to the standard | 11,937 | 11,515 | 96.5% | 422 |
| Works out pack size from the description | 144 | 114 | 79.2% | 30 |
| Reads the size from the description (AI) | run on request | — | — | 370 available |

Workload: 11,547 already correct (92.1%), 510 corrected automatically (4.1%), 430 need
a person (3.4%), 55 left blank (0.4%), 758 purged and skipped.

Most pack-size differences are case packs where the text says `12 X 227GM` and Excel
holds 36. They are evidence for the "what do size and pack size mean for a case pack"
decision, not agent mistakes.

## Recording a business decision

Decisions live in `backend/app/evaluations/business_decisions.v1.yaml`, written in
stakeholder language (a test rejects internal jargon). To record an answer set
`status: DECIDED` and `decided_note`; for a blind-test decision also set
`resolution: AGENT_CORRECT` or `EXCEL_CORRECT`. Bump `version` so cached reports rebuild.
Those products then move from "awaiting" into the confirmed figures.

## Operational notes

- The report calls no model. It is built at the end of processing and cached on the job
  (`jobs.quality`); older jobs are built on first request from their stored items.
- A reviewer decision clears the cache, because it changes the workload table.
- `?refresh=true` forces a rebuild.

## AI description-reading test (on request)

`backend/app/services/ai_reading_test_service.py` · `POST`/`GET /api/jobs/{job_id}/ai-reading-test`

- **Sample:** completed products whose description states a size (370 in v0.2). The page
  lets the user test all of them or a smaller, evenly spaced sample.
- **Blind:** the AI receives only the six permitted text fields. `InferenceRequest`
  forbids any other field, so entered and legacy values cannot reach it. Its observation
  is converted by the rule engine exactly as in normal processing, then compared with the
  entered size using the same two-part scoring.
- **Outcomes:** same answer · differs in size · differs in kind of unit (both wait for a
  business decision) · the AI gave no answer (counted as tested, never as agreement) ·
  call failed (not scored, reported separately).
- **Cost control:** never runs during processing. It is started from the page behind a
  confirmation that states the number of paid calls, is refused when `AI_PROVIDER=mock`,
  and cannot be started twice at once.
- **Record:** model id, prompt version and checksum, agent version, date, and a
  per-product audit trail (`ai_reading_results`) are stored with the run.

The first real run (370 products, 0 failed calls) scored 78.9% with 60 "no answers".
Almost all 60 were correct readings such as `140克` that the rule set could not convert.
Ruleset `poc-v2` adds the local-language spellings (`克`, `公斤`, `毫升`, `公升`, `安士`,
`磅`), which also fixes the same loss in normal processing. Raw readings are now stored,
so a later rule change can be re-scored for free (`POST …/ai-reading-test/rescore`), and
`only_unanswered` re-tests just the products without an answer.

## Not built yet

- **"Knows when not to guess" score.** Shown as a decision (55 products) until the
  business confirms blank was correct.
- AI pack-size reading is not scored; only size and unit are.
- Recording decisions from the UI; today it is a reviewed change to the YAML file.
- PDF/print export of the page.
