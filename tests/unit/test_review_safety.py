import pytest
from fastapi import HTTPException

from app.api.review import _positive_decimal, _unsafe_k_only_total_proposal


def test_historical_k_only_total_proposal_is_blocked():
    item = {
        "original": {"standard_size": "70", "standard_pack_size": "5"},
        "field_proposals": {"standard_size": "350", "standard_pack_size": None},
    }

    assert _unsafe_k_only_total_proposal(item)


def test_joint_proposal_is_not_mistaken_for_old_k_only_hazard():
    item = {
        "original": {"standard_size": "550", "standard_pack_size": "1"},
        "field_proposals": {"standard_size": "55", "standard_pack_size": "10"},
    }

    assert not _unsafe_k_only_total_proposal(item)


@pytest.mark.parametrize("value", ["1.5", "NaN", "Infinity", "0", "-1"])
def test_pack_override_requires_positive_finite_whole_number(value):
    with pytest.raises(HTTPException):
        _positive_decimal(value, field="standard_pack_size", integral=True)


def test_valid_pack_override_is_canonicalized():
    assert _positive_decimal("06", field="standard_pack_size", integral=True) == "6"
