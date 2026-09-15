from app.domain.enums import BASE_UNITS, R1_DEPARTMENTS, WorkGroup
from app.domain.product import InputProduct
from app.services.normalization import blank, clean_uom


def classify(product: InputProduct, purged: bool = False) -> WorkGroup:
    if purged:
        return WorkGroup.SKIPPED_PURGED
    if product.department not in R1_DEPARTMENTS:
        return WorkGroup.OUT_OF_SCOPE

    standard_uom = clean_uom(product.standard_uom)
    if not blank(product.standard_size) and standard_uom in BASE_UNITS and not blank(product.standard_pack_size):
        return WorkGroup.A
    if not blank(product.standard_size) and standard_uom and standard_uom not in BASE_UNITS:
        return WorkGroup.B
    if blank(product.standard_size) and blank(standard_uom) and not blank(product.legacy_uom):
        return WorkGroup.B
    if blank(product.standard_size) and blank(standard_uom) and blank(product.legacy_uom):
        return WorkGroup.C
    return WorkGroup.DATA_SHAPE_ERROR
