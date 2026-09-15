from app.domain.product import InputProduct
from app.services.discrepancy_service import find_discrepancies


def product(**changes):
    values = {"row_number": 2, "item_no": "1", "department": "03_Grocery 2"}
    values.update(changes)
    return InputProduct(**values)


def test_equivalent_units_do_not_conflict():
    item = product(item_desc_eng="JUICE 1000ML", item_desc_local="JUICE 1L")
    assert find_discrepancies(item) == []


def test_different_sizes_conflict_without_choosing_winner():
    item = product(item_desc_eng="JUICE 500ML", item_desc_local="JUICE 1L")
    details = find_discrepancies(item)
    assert len(details) == 1
    assert details[0]["left"]["value"] == "500"
    assert details[0]["right"]["value"] == "1000"
