from app.domain.enums import WorkGroup
from app.domain.product import InputProduct
from app.services.classifier import classify


def product(**changes):
    values = {"row_number": 2, "item_no": "000123", "department": "03_Grocery 2"}
    values.update(changes)
    return InputProduct(**values)


def test_group_a_requires_complete_base_fields():
    assert classify(product(standard_size="500", standard_uom="ML", standard_pack_size="1")) == WorkGroup.A


def test_group_b1_has_non_base_standard_uom():
    assert classify(product(standard_size="1", standard_uom="KG", legacy_size="1", legacy_uom="KG")) == WorkGroup.B


def test_group_b2_uses_legacy_uom():
    assert classify(product(legacy_size="500", legacy_uom="G")) == WorkGroup.B


def test_group_c_has_no_legacy_uom():
    assert classify(product(item_desc_eng="GREEN TEA")) == WorkGroup.C


def test_unexpected_shape_is_error():
    assert classify(product(standard_size="500")) == WorkGroup.DATA_SHAPE_ERROR


def test_purge_wins_before_classification():
    assert classify(product(), purged=True) == WorkGroup.SKIPPED_PURGED
