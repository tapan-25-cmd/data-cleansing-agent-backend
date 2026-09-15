# Deterministic rule maintenance

`unit_mappings.v1.yaml` is the sole runtime source for deterministic conversions.
There is no database editor or mapping approval screen.

To change a rule:

1. Confirm the business decision and record its reference in `notes`.
2. Edit the YAML and increment its top-level `version`.
3. Add or update tests covering the exact conversion.
4. Run the backend test suite and baseline-workbook profile test.
5. Merge through code review.

Rules are validated at startup. Duplicate identifiers, duplicate enabled source units,
invalid target units, non-positive factors, and unsupported operations stop startup.
`FZ`, `ST`, `SET`, `PR`, and `AV KG` remain intentionally absent until their business
rules are confirmed.
