from decimal import Decimal

from app.domain.product import InputProduct
from app.services.packaging_expression_service import (
    PACKAGING_EVIDENCE_FIELDS,
    extract_packaging_expressions,
    matching_interpretations,
)


def product(**changes) -> InputProduct:
    values = {"row_number": 2, "item_no": "240929"}
    values.update(changes)
    return InputProduct(**values)


def test_extracts_multilevel_case_expression_from_approved_web_description():
    expressions = extract_packaging_expressions(product(
        web_description_eng="5 CASE/6 X 90GM",
    ))

    assert len(expressions) == 1
    expression = expressions[0]
    assert expression.outer_count == 5
    assert expression.inner_count == 6
    assert expression.measurement == Decimal("90")
    assert matching_interpretations(
        expression,
        standard_size=Decimal("450"),
        standard_uom="GM",
        standard_pack_size=Decimal("6"),
    ) == ("OUTER_TOTAL",)


def test_extracts_single_level_case_expression():
    expression = extract_packaging_expressions(product(
        item_desc_eng="CASE/12 X 185GM",
    ))[0]

    assert expression.outer_count is None
    assert matching_interpretations(
        expression,
        standard_size=Decimal("185"),
        standard_uom="GM",
        standard_pack_size=Decimal("12"),
    ) == ("PER_UNIT",)


def test_kiki_values_do_not_match_any_approved_interpretation():
    expression = extract_packaging_expressions(product(
        web_description_eng="5 CASE/10 X 90GM",
    ))[0]

    assert matching_interpretations(
        expression,
        standard_size=Decimal("18"),
        standard_uom="GM",
        standard_pack_size=Decimal("50"),
    ) == ()


def test_derived_excel_columns_b_and_c_cannot_enter_packaging_evidence_contract():
    assert {attribute for _, attribute in PACKAGING_EVIDENCE_FIELDS} == {
        "item_desc_eng",
        "item_desc_local",
        "web_description_eng",
        "web_description_chi",
    }
    assert "product_description" not in InputProduct.model_fields
    assert "product_description_local" not in InputProduct.model_fields
