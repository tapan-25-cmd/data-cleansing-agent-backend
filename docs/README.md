# Project documentation

- [`implementation-plan.md`](implementation-plan.md) — complete Release 1 POC business, architecture, processing, validation, UI, export, and testing plan.
- [`adk-agent-design.md`](adk-agent-design.md) — Google ADK/Gemini agent boundary, schemas, runtime, validation, and test design.
- [`uom-cleansing-improvement-plan-v2.md`](uom-cleansing-improvement-plan-v2.md) — proposed explainable result ledger, human review, packaging-intelligence, discrepancy, and accuracy roadmap based on the real v0.2 job.

These documents originated while the application was a monorepo. The code is now split between this backend repository and the companion [`-data-cleansing-agent-frontend`](https://github.com/tapan-25-cmd/-data-cleansing-agent-frontend) repository. Historical monorepo layouts in the implementation plan are retained as design context; current backend paths start at `app/` and `tests/`.
