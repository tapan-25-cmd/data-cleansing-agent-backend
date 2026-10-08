"""Finding codes shared by the reasoning stage and the result ledger (no imports, so the
ledger can use them without a cycle)."""

# Findings where a rule had to interpret what a number means. They trigger the reasoning
# layer, and they are what a CONFIRMED outcome clears.
TRIGGER_CODES = frozenset({
    "DESCRIPTION_SIZE_DIFFERS",
    "DESCRIPTION_MEASUREMENT_MISMATCH",
    "DESCRIPTION_PACK_COUNT_DIFFERS",
    "DESCRIPTION_COUNT_SUGGESTS_PACK",
    "CASE_SIZE_IS_INNER_PACK",
    "PACKAGING_HIERARCHY_AMBIGUOUS",
    "SIGNIFICANT_LEGACY_SIZE_MISMATCH",
    "LEGACY_UOM_MISMATCH",
    "LINKED_SIZE_AND_PACK_SUGGESTION",
    "BILINGUAL_DESCRIPTION_CONFLICT",
    "PACK_COUNT_CONFLICT",
    "TEXT_CONTRADICTS_RESULT",
    "LEGACY_MAY_BE_PACK_TOTAL",
    "TEXT_COUNT_DIFFERS_FROM_PACK",
})

# What the reasoning layer adds to a row when its outcome acts (gate mode).
REASONER_REVIEW_CODES = frozenset({"REASONER_DISAGREES", "REASONER_OPEN_DECISION", "REASONER_UNSUPPORTED"})
