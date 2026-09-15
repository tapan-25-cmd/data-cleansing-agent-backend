from app.services.purge_detector import PURGE_FIELDS, is_purged


def test_all_fourteen_fields_blank_is_purged():
    assert len(PURGE_FIELDS) == 14
    assert is_purged({field: "  " for field in PURGE_FIELDS})


def test_one_populated_field_keeps_row_live():
    row = {field: None for field in PURGE_FIELDS}
    row[PURGE_FIELDS[-1]] = "section"
    assert not is_purged(row)
