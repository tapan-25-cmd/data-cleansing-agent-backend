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

## Rounding and Group A validation

Group B1/B2 unit-conversion proposals use Excel-equivalent nearest-whole rounding
(`ROUND_HALF_UP` at zero decimal places). The exact `raw_target` and rounded
`final_target` are both retained in proposal provenance. This processing policy is
applied by the Group B pipeline rather than as a global ruleset default, so Group C can
retain an independent policy.

Existing Group A K/L/M values are never rounded or rewritten. A complete row qualifies
for A only after deterministic validation confirms a positive finite size, exact
canonical base UOM, and positive whole-number pack size. Known spelling/casing aliases
are routed to B3 canonicalization instead of being silently accepted as A.
B3 is cleanup rather than measurement conversion, so it preserves the existing numeric
K/M values and proposes only the fields whose representation is non-canonical.
