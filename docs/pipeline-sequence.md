# Pipeline sequence

Every call, database write and AI call the tool makes, from upload to download. The diagrams
follow the code as it is on 24 September 2026. The same file is shown on the **Sequence** tab.

**Who takes part**

| Name in the diagrams | What it is |
|---|---|
| User | the person in the browser |
| Frontend | the React app |
| API | the FastAPI server (`backend/app/api`) |
| Worker | a background task in the API process: `JobProcessor`, `ExportService` or a report builder |
| Files | the file store on the server (`data/uom-jobs/<job>/input.xlsx`, `output.xlsx`) |
| MongoDB | the results database (MongoDB Atlas) |
| Gemini | the AI model, called through Google ADK |

**Numbers from the v0.2 workbook (job 9ea8ad60)**: 66,082 rows in the file, 13,300 in the two
departments, 758 purged, 12,542 live. 71 AI calls (55 description reads, 16 pack-count reads),
about 197,000 input and 21,000 output tokens. 13,300 result rows written.

## 1. High level

The whole journey in one picture. The sections below open each step.

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant FE as Frontend
    participant API
    participant W as Worker
    participant FS as Files
    participant DB as MongoDB
    participant AI as Gemini

    User->>FE: Attach the workbook
    FE->>API: POST /api/jobs (file, departments)
    API->>FS: Save input.xlsx
    API->>DB: jobs.insert_one (status UPLOADED)
    FE->>API: POST /api/jobs/{id}/process
    API->>DB: job_items.delete_many, jobs.update_one (VALIDATING)
    API-)W: Start processing in the background
    API-->>FE: 202 VALIDATING

    loop Every 2.5 s while the job runs
        FE->>API: GET /api/jobs/{id}
        API->>DB: jobs.find_one
        API-->>FE: status and progress
    end

    W->>FS: Read input.xlsx (streamed)
    W->>W: Check every row, choose a method, convert, complete
    W->>AI: Read descriptions and pack counts (71 calls, 5 at a time)
    AI-->>W: Readings with quoted evidence
    W->>W: One label per row, then the group (A, B, C, Purged)
    W->>DB: job_items.bulk_write (13,300 rows, 1,000 per batch)
    W->>DB: jobs.update_one (READY_FOR_REVIEW, stats, quality)

    FE->>API: GET results, facets, preview
    API->>DB: job_items.find / count_documents
    API-->>FE: Rows with group, method, how, status

    opt A person decides a raised row
        User->>FE: Keep Excel, use the suggestion, or enter values
        FE->>API: PATCH /api/jobs/{id}/items/{row}/decision
        API->>DB: job_items.update_one, jobs.update_one
    end

    User->>FE: Generate the workbook
    FE->>API: POST /api/jobs/{id}/export
    API->>DB: jobs.update_one (EXPORTING)
    API-)W: Build the workbook in the background
    W->>DB: job_items.find (export projection)
    W->>FS: Read input.xlsx, write output.xlsx
    W->>DB: jobs.update_one (EXPORTED, export_version)
    FE->>API: GET /api/jobs/{id}/download
    API->>FS: Read output.xlsx
    API-->>User: <name>-cleansed.xlsx
```

## 2. Server start

Runs once when the API starts.

```mermaid
sequenceDiagram
    participant API
    participant FS as Files
    participant DB as MongoDB

    API->>API: Load settings and the unit ruleset (YAML)
    API->>DB: Connect, create indexes (jobs, job_items and the report collections)
    API->>FS: Open the file store
    API->>API: Build the Gemini provider (ADK runner, in-memory sessions)
    API->>API: Build JobProcessor, ExportService, test services, chat
```

## 3. Upload

```mermaid
sequenceDiagram
    actor User
    participant FE as Frontend
    participant API
    participant FS as Files
    participant DB as MongoDB

    User->>FE: Type "Please do the unit measurement"
    FE->>API: POST /api/chat/messages
    API-->>FE: Please upload the workbook (no AI call, fixed wording)
    User->>FE: Attach 20260908_UoM_Snapshot_v0.2.xlsx
    FE->>API: POST /api/jobs (file, departments 03_Grocery 2 and 06_Dairy & Frozen)
    API->>API: Only .xlsx, only the two Release 1 departments
    API->>FS: save_input (size limit checked while writing)
    API->>FS: Open the workbook, check the sheet and headers
    alt File cannot be used
        API-->>FE: 422 with the reason
    else File is good
        API->>DB: jobs.insert_one (UPLOADED, departments, ruleset version, v0.2 label)
        API-->>FE: 201 job_id
    end
```

## 4. Start processing

```mermaid
sequenceDiagram
    participant FE as Frontend
    participant API
    participant DB as MongoDB
    participant W as Worker

    FE->>API: POST /api/jobs/{id}/process
    API->>DB: jobs.find_one
    API->>DB: dbstats (storage used)
    alt Less than 40 MB free
        API-->>FE: 507 The results database is full
    end
    API->>API: A job silent for 10 minutes is marked FAILED so it can be restarted
    alt Status is not UPLOADED or FAILED
        API-->>FE: 409
    end
    API->>DB: job_items.delete_many (rows of an interrupted run)
    API->>DB: jobs.update_one (VALIDATING, ruleset version)
    API-)W: JobProcessor.process(job_id)
    API-->>FE: 202 VALIDATING
```

## 5. Processing

The worker runs inside the API process. Progress is written to the job at most every 0.75 s,
which is what the chat's progress bar shows. Nothing is written to `job_items` until every row
is finished.

```mermaid
sequenceDiagram
    participant W as Worker
    participant FS as Files
    participant DB as MongoDB
    participant AI as Gemini

    W->>DB: jobs.find_one, jobs.update_one (PROCESSING)

    Note over W: Stage PROFILING
    W->>FS: Stream input.xlsx row by row (66,082 rows)
    W->>DB: jobs.update_one progress (throttled)
    W->>W: Keep the rows of the chosen departments (13,300)

    Note over W: Pass 1, check every row
    loop Each row that is not purged
        W->>W: Read sizes and counts from the six descriptions (English and Chinese)
        W->>W: Check K, L, M against the old size and the descriptions
    end
    W->>W: Learn each category's usual units from the rows that passed

    Note over W: Stage PROCESSING_RULES, choose a method per row
    loop Each row
        alt Purged
            W->>W: Skip. Nothing read or changed
        else K, L, M all filled
            W->>W: Keep, note or raise (checker result)
        else Unit spelling or a number stored as text
            W->>W: Tidy it (for example G to GM)
        else Old size only
            W->>W: Convert with the unit table, run the safety checks
            W->>W: Look for a pack count in the text
        else Nothing usable
            W->>W: Queue for the description reader
        else Half-filled
            W->>W: Fill the missing values from the old size or the text, check the result
        end
        W->>DB: jobs.update_one progress (throttled)
    end

    Note over W,AI: Stage PROCESSING_DESCRIPTIONS, at most 5 calls at a time
    par Pack counts the rules could not settle (16 calls)
        W->>AI: Six descriptions, category, known size and unit
        AI-->>W: Pack count with the quoted words, or none
    and Rows with nothing usable (55 calls)
        W->>AI: Six descriptions and the category only
        AI-->>W: Size, unit and pack with the quoted words, or nothing found
    end
    W->>W: Reject any answer whose quote is not in the text, run the safety checks
    W->>DB: jobs.update_one progress after each call

    Note over W: Label and group
    W->>W: One label per row, findings and the K, L, M change record
    W->>W: Group from the label: A no change, B changed, C raised, Purged
    W->>W: Count by method and by group, check the v0.2 method counts

    Note over W,DB: Stage SAVING_RESULTS
    loop 14 batches of up to 1,000 rows
        W->>DB: job_items.bulk_write (replace by job and row, upsert)
        W->>DB: jobs.update_one progress
    end
    W->>DB: jobs.update_one (READY_FOR_REVIEW, stats, groups, rule readiness, AI usage, quality report)

    opt Any step fails
        W->>DB: jobs.update_one (FAILED, error)
    end
```

### One AI call in detail

```mermaid
sequenceDiagram
    participant W as Worker
    participant ADK as ADK runner
    participant AI as Gemini

    W->>ADK: New in-memory session (one per call)
    W->>ADK: The request as JSON
    ADK->>AI: Prompt and request (timeout 60 s)
    AI-->>ADK: Structured answer
    ADK-->>W: Answer, token counts
    alt Answer does not fit the schema
        W->>ADK: Same request with the validation error (one repair attempt)
        ADK->>AI: Retry
        AI-->>ADK: Structured answer
    end
    alt Rate limit or outage (408, 429, 5xx)
        W->>W: Wait 1.5 s, 3 s, 6 s, up to 30 s, with jitter (up to 4 retries)
        W->>ADK: Retry
    end
    alt Still failing
        W->>W: Description read: the row is raised as Could not determine, the error is recorded
        W->>W: Pack-count read: the pack count is left unset, the error is recorded
    end
```

## 6. Reading results

Every tab reads. None of these calls change a row.

```mermaid
sequenceDiagram
    participant FE as Frontend
    participant API
    participant DB as MongoDB

    FE->>API: GET /api/jobs/{id}/summary, /preview
    API->>DB: jobs.find_one, job_items.find (3 rows per group)
    FE->>API: GET /api/jobs/{id}/result-facets
    API->>DB: job_items.aggregate (method, review, findings), count_documents per label
    API-->>FE: Counts per label, per group, per method
    FE->>API: GET /api/jobs/{id}/results?group=C&route=A&search=milk
    API->>DB: job_items.find (all filters joined with $and), count_documents
    API-->>FE: Rows with label, group, method and how, worked out on read
    FE->>API: GET /api/jobs/{id}/items/{row}
    API->>DB: job_items.find_one
```

## 7. A person decides a raised row

```mermaid
sequenceDiagram
    actor User
    participant FE as Frontend
    participant API
    participant DB as MongoDB

    User->>FE: Keep Excel, use the suggestion, or enter values
    FE->>API: PATCH /api/jobs/{id}/items/{row}/decision
    API->>DB: job_items.find (the row)
    API->>API: Check the values (GM, ML or EA, positive, whole pack)
    API->>DB: job_items.update_one (review decision, change record)
    API->>DB: count rows still pending
    API->>DB: jobs.update_one (REVIEW_IN_PROGRESS or READY_TO_EXPORT, clear the quality report)
    API-->>FE: Decision saved
    Note over API,DB: The row keeps its group. A decided row stays in Group C.
```

## 8. Export and download

```mermaid
sequenceDiagram
    actor User
    participant FE as Frontend
    participant API
    participant W as Worker
    participant FS as Files
    participant DB as MongoDB

    User->>FE: Generate and download the workbook
    FE->>API: POST /api/jobs/{id}/export
    API->>DB: jobs.find_one
    alt Already exporting
        API-->>FE: 409
    end
    API->>DB: jobs.update_one (EXPORTING)
    API-)W: ExportService.export(job_id)
    API-->>FE: 202 EXPORTING
    W->>DB: job_items.find (values, proposals, provenance, decisions, findings)
    W->>FS: Open input.xlsx as a zip
    W->>W: Patch K, L, M in the sheet XML, add Group, How, Status, Comment and audit columns
    W->>W: Add one fill colour per label to the styles
    W->>FS: Write a new zip, copy every other part unchanged
    W->>W: Open the new file to check it is valid
    W->>FS: save_output (output.xlsx)
    W->>DB: jobs.update_one (EXPORTED, export_version)
    Note over W,DB: Progress stages are written along the way
    FE->>API: GET /api/jobs/{id}/download
    API->>DB: jobs.find_one
    alt Built by an earlier export version
        API-->>FE: 409 Generate it again
    else Current
        API->>FS: Read output.xlsx
        API-->>User: <name>-cleansed.xlsx
    end
```

## 9. Reports built on demand

These tabs build their figures in the background the first time they are opened, keep them in
the database, and rebuild when the job or the report's version changes. The page shows the last
figures while a rebuild runs.

```mermaid
sequenceDiagram
    participant FE as Frontend
    participant API
    participant W as Worker
    participant DB as MongoDB

    Note over FE,DB: Accuracy
    FE->>API: GET /api/jobs/{id}/accuracy
    API->>DB: job_accuracy.find_one, lane_a_trials.find_one
    alt No report, or built for another job version or report version
        API->>DB: job_accuracy.replace_one (BUILDING)
        API-)W: Build the accuracy report
        API-->>FE: 202, or the previous report marked refreshing
        W->>DB: job_items.find (every live row, narrow), lane_a_trial_rows.find
        W->>DB: job_accuracy.replace_one (READY, report, rows per set)
    else Current
        API-->>FE: The report
    end
    FE->>API: GET /api/jobs/{id}/accuracy/{set}
    API->>DB: job_accuracy.find_one, job_items.find (that page of rows)

    Note over FE,DB: Past vs New
    FE->>API: GET /api/jobs/{id}/comparison
    API->>DB: jobs.find (the earlier run), job_comparisons.find_one
    alt Not built, or built for another version
        API->>DB: job_comparisons.replace_one (BUILDING)
        API-)W: Compare the two runs row by row
        W->>DB: job_items.find (past run), job_items.find (new run)
        W->>DB: job_comparison_rows.delete_many, insert_many (1,000 per batch)
        W->>DB: job_comparisons.replace_one (READY, summary)
    end
    API->>DB: job_comparison_rows.find (filtered page)

    Note over FE,DB: Open questions
    FE->>API: GET /api/jobs/{id}/open-questions
    API->>DB: job_items.find (rows that are not simply already correct)
    API->>API: Group them by question, keep in memory for this job version
    API->>DB: open_question_answers.find
    FE->>API: PUT /api/jobs/{id}/open-questions/{category}/answer
    API->>DB: open_question_answers.replace_one

    Note over FE,DB: Agent performance
    FE->>API: GET /api/jobs/{id}/quality
    API->>DB: job_items.find (quality projection)
    API->>DB: jobs.update_one (quality report, when it was cleared)
```

## 10. Tests and trials

Started by a person, never by the pipeline. The ones that call Gemini are paid.

```mermaid
sequenceDiagram
    actor User
    participant API
    participant W as Worker
    participant DB as MongoDB
    participant AI as Gemini

    Note over User,AI: Blind tests (A and B free, C one call per product)
    User->>API: POST /api/jobs/{id}/blind-test/{kind}/run
    API->>DB: blind_tests.replace_one (RUNNING)
    API-)W: Run the test
    W->>DB: job_items.find
    opt Reading test (C)
        W->>AI: Descriptions only, the answer hidden
    end
    W->>DB: blind_tests.replace_one (READY, score, rows)

    Note over User,AI: AI reading test (paid)
    User->>API: POST /api/jobs/{id}/ai-reading-test
    API-)W: Read products whose sizes are known, with the sizes hidden
    W->>AI: One call per product
    W->>DB: ai_reading_results.delete_many, insert_many

    Note over User,AI: Hard-case gate for the reader (paid)
    User->>API: POST /api/evaluations/agent/run
    API-)W: Run the 30 hard cases
    W->>AI: One call per case
    W->>DB: agent_evaluations.insert_one

    Note over User,AI: Reasoning trial (paid, run from a script, shadow only)
    User->>W: Run the trial on the rows still open
    W->>DB: job_items.find (raised rows)
    W->>AI: Every source for the row, asks for a verdict and a proposal
    W->>DB: lane_a_trials.replace_one, lane_a_trial_rows.delete_many, insert_many
    Note over W,DB: Nothing in job_items changes. Answers are shown beside rows.
```

## 11. Job status

```mermaid
stateDiagram-v2
    [*] --> UPLOADED: POST /jobs
    UPLOADED --> VALIDATING: POST /process
    VALIDATING --> PROCESSING: worker starts
    PROCESSING --> READY_FOR_REVIEW: rows saved
    PROCESSING --> FAILED: error, or silent for 10 minutes
    FAILED --> VALIDATING: Process again
    READY_FOR_REVIEW --> REVIEW_IN_PROGRESS: a decision, rows still pending
    REVIEW_IN_PROGRESS --> READY_TO_EXPORT: nothing pending
    READY_FOR_REVIEW --> EXPORTING: POST /export
    REVIEW_IN_PROGRESS --> EXPORTING: POST /export
    READY_TO_EXPORT --> EXPORTING: POST /export
    EXPORTING --> EXPORTED: output.xlsx stored
    EXPORTED --> EXPORTING: export again
```

## 12. Every database write

| Collection | Operation | When | By |
|---|---|---|---|
| `jobs` | `insert_one` | upload | API |
| `jobs` | `update_one` | start, every progress step (at most every 0.75 s), finish, failure | API, Worker |
| `jobs` | `update_one` | each review decision (status, quality cleared) | API |
| `jobs` | `update_one` | export start, each export stage, export done | API, Worker |
| `jobs` | `update_one` | quality report rebuilt after decisions or sample verifications | API |
| `job_items` | `delete_many` | before a run starts (rows of an interrupted run) | API |
| `job_items` | `bulk_write` of `ReplaceOne` with upsert | end of a run, 1,000 rows per batch | Worker |
| `job_items` | `update_one` | a review decision or a sample verification | API |
| `job_items` | `update_many` | approving a whole conversion rule | API |
| `job_accuracy` | `replace_one` | accuracy build start and finish | API, Worker |
| `job_comparisons` | `replace_one` | comparison build start and finish | API, Worker |
| `job_comparison_rows` | `delete_many`, `insert_many` | comparison build | Worker |
| `open_question_answers` | `replace_one` | an answer to an open question | API |
| `blind_tests` | `replace_one` | blind test start and finish | API, Worker |
| `ai_reading_results` | `delete_many`, `insert_many` | AI reading test | Worker |
| `agent_evaluations` | `insert_one` | hard-case gate run | Worker |
| `lane_a_trials`, `lane_a_trial_rows` | `replace_one`, `delete_many`, `insert_many` | reasoning trial | script |

The files are written in two places only: `input.xlsx` at upload and `output.xlsx` at export.

## 13. Every AI call

| Call | Sees | Returns | When | Count on v0.2 |
|---|---|---|---|---|
| Description reader | six descriptions, category as context | size, unit, pack, quoted words | rows with nothing usable | 55 |
| Pack-count reader | six descriptions, category, known size and unit | pack count, quoted words | converted rows whose pack count the rules could not settle | 16 |
| Reading test | six descriptions, answer hidden | same as the reader | a person starts it | one per product tested |
| Hard-case gate | 30 fixed cases | same as the reader | a person starts it | 30 |
| Reasoning trial | every source for the row | verdict, proposal, explanation | a person runs the script | one per open row |

All calls go through the same provider: one ADK session per call, 60 s timeout, one repair
attempt for an answer that does not fit the schema, up to four retries with backoff for rate
limits and outages, at most five calls at a time. An answer is used only if the words it quotes
are really in the product's text.
