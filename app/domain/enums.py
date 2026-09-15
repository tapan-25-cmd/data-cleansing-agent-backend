from enum import Enum


class WorkGroup(str, Enum):
    A = "A"
    B = "B"
    C = "C"
    SKIPPED_PURGED = "SKIPPED_PURGED"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    DATA_SHAPE_ERROR = "DATA_SHAPE_ERROR"


class ProposalMethod(str, Enum):
    NONE = "NONE"
    RULE = "RULE"
    AI_INFERENCE = "AI_INFERENCE"
    HUMAN_OVERRIDE = "HUMAN_OVERRIDE"


class Decision(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    OVERRIDDEN = "OVERRIDDEN"


BASE_UNITS = frozenset({"EA", "GM", "ML", "FT"})
R1_DEPARTMENTS = frozenset({"03_Grocery 2", "06_Dairy & Frozen"})
