"""Discrepancy engine v2: multi-signal, bilingual, packaging-aware comparison.

The engine extracts *every* explicit measurement and count from the six
PRD-approved independent text fields, then compares English against the local
language within exactly three pairs (FR-18): brand, item description, and web
description. Pairs are never crossed: brand text is not compared with a
description, and item description is not compared with web description.

It never picks a winner and never proposes K/L/M. A conflict is evidence for a
human reviewer only. Excel columns B/C cannot be read here because they are not
part of ``InputProduct``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable

from app.domain.product import InputProduct


DISCREPANCY_ENGINE_VERSION = "discrepancy-v2"

# evidence field label -> InputProduct attribute
EVIDENCE_FIELDS = (
    ("item_brand_eng", "item_brand_eng"),
    ("item_brand_local_lang", "item_brand_local"),
    ("item_desc_eng", "item_desc_eng"),
    ("item_desc_local_lang", "item_desc_local"),
    ("web_description_eng", "web_description_eng"),
    ("web_description_chi", "web_description_chi"),
)
BILINGUAL_PAIRS = (
    ("brand", ("item_brand_eng",), ("item_brand_local_lang",)),
    ("item_desc", ("item_desc_eng",), ("item_desc_local_lang",)),
    ("web_desc", ("web_description_eng",), ("web_description_chi",)),
)

# Converted comparisons (for example 16 OZ versus 454 GM) tolerate the same one
# base-unit rounding difference that Group A validation already accepts.
CONVERSION_TOLERANCE = Decimal("1")

# token -> (source UOM, normalized UOM, dimension, factor)
_UNITS: dict[str, tuple[str, str, str, Decimal]] = {
    "KG": ("KG", "GM", "WEIGHT", Decimal("1000")),
    "GM": ("GM", "GM", "WEIGHT", Decimal("1")),
    "G": ("G", "GM", "WEIGHT", Decimal("1")),
    "OZ": ("OZ", "GM", "WEIGHT", Decimal("28.349523125")),
    "LB": ("LB", "GM", "WEIGHT", Decimal("453.59237")),
    "LBS": ("LB", "GM", "WEIGHT", Decimal("453.59237")),
    "ML": ("ML", "ML", "VOLUME", Decimal("1")),
    "LT": ("LT", "ML", "VOLUME", Decimal("1000")),
    "LTR": ("LT", "ML", "VOLUME", Decimal("1000")),
    "L": ("L", "ML", "VOLUME", Decimal("1000")),
    # Traditional/Simplified Chinese units used in the local-language fields.
    "公斤": ("KG", "GM", "WEIGHT", Decimal("1000")),
    "千克": ("KG", "GM", "WEIGHT", Decimal("1000")),
    "克": ("GM", "GM", "WEIGHT", Decimal("1")),
    "安士": ("OZ", "GM", "WEIGHT", Decimal("28.349523125")),
    "磅": ("LB", "GM", "WEIGHT", Decimal("453.59237")),
    "毫升": ("ML", "ML", "VOLUME", Decimal("1")),
    "公升": ("L", "ML", "VOLUME", Decimal("1000")),
    "升": ("L", "ML", "VOLUME", Decimal("1000")),
}
_LATIN_UNITS = "KG|GM|G|OZ|LBS|LB|ML|LTR|LT|L"
_CJK_UNITS = "公斤|千克|毫升|公升|安士|克|升|磅"
# A Latin token ends at a non-letter, or at a multiplier such as ``500MLX2``.
_LATIN_END = r"(?![A-WYZ])(?!X(?!\s*\d))"
_UNIT = rf"(?:(?:{_LATIN_UNITS}){_LATIN_END}|(?:{_CJK_UNITS}))"
_NUMBER = r"\d+(?:\.\d+)?"

_MEASUREMENT = re.compile(
    rf"(?<![\d.])(?P<value>{_NUMBER})\s*"
    rf"(?:(?P<latin>{_LATIN_UNITS}){_LATIN_END}|(?P<cjk>{_CJK_UNITS}))",
    re.IGNORECASE,
)

_CJK_CLASSIFIER = "連包|包|支|枝|件|杯|粒|個|个|隻|只|片|罐|條|条|盒|瓶|樽|塊|袋|份|卷|入"
_CJK_NUMERALS = {
    "一": 1, "二": 2, "兩": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_COUNT = r"(?P<count>\d{1,3})"
_PACK_WORDS = (
    r"(?:['’]\s*S|PIECES?|PCS?|PACKS?|PKS?|CUPS?|CANS?|BOTTLES?|BTLS?|SACHETS?|STICKS?|BAGS?|CT)"
    + _LATIN_END
)
_COUNT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # ``20 X 350GM``, ``24 X 1PC`` and the web shorthand ``Noodle 5 x``.
    ("MULTIPLIER", re.compile(
        rf"(?<![\d.]){_COUNT}\s*[x×*](?:\s*(?=\d)|(?![A-Z]))", re.IGNORECASE,
    )),
    # Unitless ``4X60`` / ``150X2`` does not say which factor is the count, so
    # both stay candidates. Extra candidates can only remove false conflicts.
    ("MULTIPLIER_UNITLESS", re.compile(
        rf"\d\s*[x×*]\s*{_COUNT}(?![\d.])(?!\s*{_UNIT})", re.IGNORECASE,
    )),
    # Glued to a word: ``GOLD LABELX2``.
    ("MULTIPLIER", re.compile(
        rf"(?<=[A-Z])[x×]{_COUNT}(?![\d.])(?!\s*{_UNIT})", re.IGNORECASE,
    )),
    ("PACK_WORD", re.compile(
        rf"(?<![\d.]){_COUNT}\s*(?:{_PACK_WORDS})", re.IGNORECASE,
    )),
    ("PACK_WORD", re.compile(
        rf"(?<![\d.]){_COUNT}(?:PS|S|P){_LATIN_END}", re.IGNORECASE,
    )),
    ("PACK_NOTATION", re.compile(rf"\\\s*{_COUNT}(?![\d.])")),
    ("OUTER_CASE", re.compile(rf"(?<![\d.]){_COUNT}\s*(?:CASE|CS)\b", re.IGNORECASE)),
    ("PACK_WORD", re.compile(
        rf"(?<![\d.]){_COUNT}\s*(?:{_CJK_CLASSIFIER})(?!月)",
    )),
)
_CJK_NUMERAL_COUNT = re.compile(
    rf"(?P<numeral>[一二兩两三四五六七八九十]{{1,3}})(?:{_CJK_CLASSIFIER})(?:裝|装|入)"
)
_TWIN = re.compile(r"孖裝|孖装|\bTWIN\s*PACK\b", re.IGNORECASE)
_MEASUREMENT_X_COUNT = re.compile(
    rf"\s*[x×]\s*{_COUNT}(?![\d.])(?!\s*{_UNIT})", re.IGNORECASE,
)


@dataclass(frozen=True)
class MeasurementSignal:
    field: str
    fragment: str
    start: int
    end: int
    source_value: Decimal
    source_uom: str
    value: Decimal  # normalized to GM or ML
    uom: str
    dimension: str

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": "MEASUREMENT",
            "field": self.field,
            "fragment": self.fragment,
            "start": self.start,
            "end": self.end,
            "source_value": str(self.source_value),
            "source_uom": self.source_uom,
            "value": _plain(self.value),
            "uom": self.uom,
            "dimension": self.dimension,
        }


@dataclass(frozen=True)
class CountSignal:
    field: str
    fragment: str
    start: int
    end: int
    value: int
    qualifier: str

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": "COUNT",
            "field": self.field,
            "fragment": self.fragment,
            "start": self.start,
            "end": self.end,
            "value": self.value,
            "qualifier": self.qualifier,
        }


@dataclass(frozen=True)
class FieldSignals:
    field: str
    measurements: tuple[MeasurementSignal, ...] = ()
    counts: tuple[CountSignal, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.measurements or self.counts)

    def as_dict(self) -> dict[str, object]:
        return {
            "measurements": [signal.as_dict() for signal in self.measurements],
            "counts": [signal.as_dict() for signal in self.counts],
        }


def _plain(value: Decimal) -> str:
    """Render 1000 rather than 1E+3 and 500 rather than 500.0."""
    text = format(value.normalize(), "f")
    return text


def _chinese_number(text: str) -> int | None:
    if not text:
        return None
    if "十" not in text:
        return _CJK_NUMERALS.get(text) if len(text) == 1 else None
    tens, _, units = text.partition("十")
    result = (_CJK_NUMERALS.get(tens, 0) if tens else 1) * 10
    if units:
        if units not in _CJK_NUMERALS or units == "十":
            return None
        result += _CJK_NUMERALS[units]
    return result


def extract_field_signals(field: str, text: str | None) -> FieldSignals:
    """Return every explicit measurement and count with its literal fragment."""
    if not text:
        return FieldSignals(field)
    measurements: list[MeasurementSignal] = []
    counts: dict[int, CountSignal] = {}

    def add_count(start: int, end: int, number_start: int, value: int, qualifier: str) -> None:
        if value <= 0 or number_start in counts:
            return
        if any(signal.start <= number_start < signal.end for signal in measurements):
            return
        counts[number_start] = CountSignal(
            field, text[start:end], start, end, value, qualifier,
        )

    for match in _MEASUREMENT.finditer(text):
        token = (match.group("latin") or match.group("cjk")).upper()
        source_uom, uom, dimension, factor = _UNITS[token]
        source_value = Decimal(match.group("value"))
        if source_value <= 0:
            continue
        measurements.append(MeasurementSignal(
            field, match.group(0), match.start(), match.end(),
            source_value, source_uom, source_value * factor, uom, dimension,
        ))
    for signal in tuple(measurements):
        trailing = _MEASUREMENT_X_COUNT.match(text, signal.end)
        if trailing:
            add_count(
                signal.start, trailing.end(), trailing.start("count"),
                int(trailing.group("count")), "MULTIPLIER",
            )
    for qualifier, pattern in _COUNT_PATTERNS:
        for match in pattern.finditer(text):
            add_count(
                match.start(), match.end(), match.start("count"),
                int(match.group("count")), qualifier,
            )
    for match in _CJK_NUMERAL_COUNT.finditer(text):
        value = _chinese_number(match.group("numeral"))
        if value is not None:
            add_count(match.start(), match.end(), match.start("numeral"), value, "PACK_WORD")
    for match in _TWIN.finditer(text):
        add_count(match.start(), match.end(), match.start(), 2, "TWIN_PACK")
    return FieldSignals(
        field,
        tuple(measurements),
        tuple(counts[key] for key in sorted(counts)),
    )


def extract_product_signals(product: InputProduct) -> dict[str, FieldSignals]:
    return {
        field: extract_field_signals(field, getattr(product, attribute))
        for field, attribute in EVIDENCE_FIELDS
    }


def values_equivalent(left: Decimal, right: Decimal, *, converted: bool) -> bool:
    if left == right:
        return True
    return converted and abs(left - right) <= CONVERSION_TOLERANCE


def measurements_equivalent(left: MeasurementSignal, right: MeasurementSignal) -> bool:
    if left.dimension != right.dimension:
        return False
    converted = left.source_uom != left.uom or right.source_uom != right.uom
    return values_equivalent(left.value, right.value, converted=converted)


def matches_value(value: Decimal, target: Decimal, *, converted: bool) -> bool:
    """Exact, nearest-whole, or (for converted units) one-unit-tolerant match."""
    if value == target:
        return True
    if value.quantize(Decimal("1"), rounding=ROUND_HALF_UP) == target:
        return True
    return converted and abs(value - target) <= CONVERSION_TOLERANCE


def text_check(
    signals: Iterable[FieldSignals],
    size: Decimal,
    uom: str,
    pack_size: Decimal | None = None,
) -> tuple[str, list[str]]:
    """Does independent product text confirm a size? -> (verdict, fragments read).

    CONFIRMED when any field states the size, per unit or as the whole-pack total;
    CONTRADICTED when the text states sizes and none fits; NO_TEXT when the text
    states no weight or volume at all (or the value is a count, which text cannot
    confirm)."""
    dimension = {"GM": "WEIGHT", "ML": "VOLUME"}.get(uom)
    fields = [field for field in signals if field.measurements]
    fragments = [m.fragment for field in fields for m in field.measurements]
    if dimension is None or not fields:
        return "NO_TEXT", fragments
    accepted = {size} | ({size * pack_size} if pack_size else set())
    for field in fields:
        converted = any(m.source_uom != m.uom for m in field.measurements)
        for level_dimension, value in packaging_levels(field.measurements, field.counts):
            if level_dimension == dimension and any(
                matches_value(value, target, converted=converted) for target in accepted
            ):
                return "CONFIRMED", fragments
    return "CONTRADICTED", fragments


def packaging_levels(
    measurements: Iterable[MeasurementSignal],
    counts: Iterable[CountSignal],
) -> set[tuple[str, Decimal]]:
    """Per-unit values plus every count-multiplied total stated in the same text."""
    measurements = tuple(measurements)
    count_values = {signal.value for signal in counts}
    levels = {(signal.dimension, signal.value) for signal in measurements}
    for signal in measurements:
        for count in count_values:
            levels.add((signal.dimension, signal.value * count))
    return levels


def _levels_overlap(
    left: set[tuple[str, Decimal]],
    right: set[tuple[str, Decimal]],
) -> bool:
    return any(
        left_dimension == right_dimension
        and abs(left_value - right_value) <= CONVERSION_TOLERANCE
        for left_dimension, left_value in left
        for right_dimension, right_value in right
    )


def _likely_typo(left: Decimal, right: Decimal) -> bool:
    small, large = sorted((left, right))
    if small > 0 and large / small in {Decimal("10"), Decimal("100"), Decimal("1000")}:
        return True
    a, b = _plain(left), _plain(right)
    if len(a) == len(b) and sorted(a) == sorted(b):
        differing = [index for index in range(len(a)) if a[index] != b[index]]
        return len(differing) == 2 and differing[1] - differing[0] == 1
    return False


def _side(signals: list[MeasurementSignal] | list[CountSignal]) -> dict[str, object]:
    first = signals[0]
    value = _plain(first.value) if isinstance(first, MeasurementSignal) else str(first.value)
    return {"field": first.field, "value": value, "fragment": first.fragment}


def _detail(
    *,
    scope: str,
    pair: str,
    aspect: str,
    status: str,
    classification: str,
    left: list,
    right: list,
) -> dict[str, object]:
    detail: dict[str, object] = {
        "engine_version": DISCREPANCY_ENGINE_VERSION,
        "scope": scope,
        "pair": pair,
        "aspect": aspect,
        "status": status,
        "classification": classification,
        "left_signals": [signal.as_dict() for signal in left],
        "right_signals": [signal.as_dict() for signal in right],
    }
    if left:
        detail["left"] = _side(left)
    if right:
        detail["right"] = _side(right)
    return detail


def _compare(
    scope: str,
    pair: str,
    left: list[FieldSignals],
    right: list[FieldSignals],
) -> list[dict[str, object]]:
    details: list[dict[str, object]] = []
    left_measurements = [signal for side in left for signal in side.measurements]
    right_measurements = [signal for side in right for signal in side.measurements]
    left_counts = [signal for side in left for signal in side.counts]
    right_counts = [signal for side in right for signal in side.counts]

    common = dict(scope=scope, pair=pair, left=left_measurements, right=right_measurements)
    if left_measurements and right_measurements:
        agree = any(
            measurements_equivalent(a, b)
            for a in left_measurements for b in right_measurements
        )
        if not agree:
            if _levels_overlap(
                packaging_levels(left_measurements, left_counts),
                packaging_levels(right_measurements, right_counts),
            ):
                details.append(_detail(
                    aspect="MEASUREMENT", status="OBSERVATION",
                    classification="PACKAGING_LEVEL_DIFFERENCE", **common,
                ))
            else:
                shared = {a.dimension for a in left_measurements} & {
                    b.dimension for b in right_measurements
                }
                fluid_ounce = any(
                    signal.source_uom == "OZ"
                    for signal in (*left_measurements, *right_measurements)
                )
                if not shared and fluid_ounce:
                    # O-2: OZ may be a fluid ounce; the factor is not approved,
                    # so a weight/volume disagreement cannot be asserted.
                    status, classification = "OBSERVATION", "FLUID_OUNCE_AMBIGUOUS"
                elif not shared:
                    status, classification = "CONFLICT", "DIMENSION_CONFLICT"
                elif any(
                    a.dimension == b.dimension and _likely_typo(a.value, b.value)
                    for a in left_measurements for b in right_measurements
                ):
                    status, classification = "CONFLICT", "LIKELY_TYPO"
                else:
                    status, classification = "CONFLICT", "VALUE_CONFLICT"
                details.append(_detail(
                    aspect="MEASUREMENT", status=status,
                    classification=classification, **common,
                ))
    elif left_measurements or right_measurements:
        # One populated side is missing data, never a conflict.
        details.append(_detail(
            aspect="MEASUREMENT", status="INSUFFICIENT",
            classification="INSUFFICIENT_COMPARISON_DATA", **common,
        ))

    if left_counts and right_counts and not (
        {signal.value for signal in left_counts} & {signal.value for signal in right_counts}
    ):
        details.append(_detail(
            scope=scope, pair=pair, aspect="COUNT", status="CONFLICT",
            classification="COUNT_CONFLICT", left=left_counts, right=right_counts,
        ))
    return details


@dataclass(frozen=True)
class DiscrepancyReport:
    signals: dict[str, FieldSignals]
    details: tuple[dict[str, object], ...]

    @property
    def conflicts(self) -> list[dict[str, object]]:
        return [detail for detail in self.details if detail["status"] == "CONFLICT"]

    @property
    def bilingual_conflicts(self) -> list[dict[str, object]]:
        return [detail for detail in self.conflicts if detail["scope"] == "BILINGUAL_PAIR"]

    @property
    def flagged(self) -> bool:
        return bool(self.conflicts)

    def as_dict(self) -> dict[str, object]:
        return {
            "version": DISCREPANCY_ENGINE_VERSION,
            "flagged": self.flagged,
            "details": list(self.details),
            "signals": {
                field: signals.as_dict()
                for field, signals in self.signals.items() if signals
            },
        }


def analyze_discrepancies(product: InputProduct) -> DiscrepancyReport:
    signals = extract_product_signals(product)
    details: list[dict[str, object]] = []
    for pair, left_fields, right_fields in BILINGUAL_PAIRS:
        details.extend(_compare(
            "BILINGUAL_PAIR", pair,
            [signals[field] for field in left_fields],
            [signals[field] for field in right_fields],
        ))
    return DiscrepancyReport(signals, tuple(details))


def find_discrepancies(product: InputProduct) -> list[dict[str, object]]:
    """Conflicts only. ``analyze_discrepancies`` also returns observations and signals."""
    return analyze_discrepancies(product).bilingual_conflicts
