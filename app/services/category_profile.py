"""What this workbook's own validated rows say is normal for each product category.

The profile is evidence from the client's data, not outside product knowledge. It is
built per workbook from rows whose size, unit and pack were entered and validated, and
it answers three questions for a product's category:

* is this category sold by weight, by volume, or by count?
* what range of sizes is normal here?
* when legacy data says "pieces", do people keep a count?

Lookups fall back section -> subcategory -> category -> department until a level has
enough rows to be trusted.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

CATEGORY_PROFILE_VERSION = "category-profile-v1"
MIN_SUPPORT = 15
# D2: a category is liquid when at least this share of its weight/volume rows are ML.
LIQUID_SHARE = Decimal("0.8")
MIXED_SHARE = Decimal("0.2")
# A proposal further than this factor outside the 2nd-98th percentile band is implausible.
RANGE_MARGIN = Decimal("3")
LEVELS = ("section", "subcategory", "category", "department")


@dataclass
class _Level:
    units: Counter = field(default_factory=Counter)
    sizes: dict[str, list[Decimal]] = field(default_factory=lambda: defaultdict(list))


@dataclass(frozen=True)
class CategoryView:
    """The profile as seen for one product, at the most specific trusted level."""

    level: str | None
    name: str | None
    rows: int
    units: dict[str, int]

    @property
    def volume_share(self) -> Decimal | None:
        measured = self.units.get("GM", 0) + self.units.get("ML", 0)
        return Decimal(self.units.get("ML", 0)) / measured if measured >= MIN_SUPPORT else None

    @property
    def liquid(self) -> bool:
        return self.volume_share is not None and self.volume_share >= LIQUID_SHARE

    @property
    def mixed(self) -> bool:
        return self.volume_share is not None and MIXED_SHARE < self.volume_share < LIQUID_SHARE

    def share(self, unit: str) -> Decimal | None:
        return Decimal(self.units.get(unit, 0)) / self.rows if self.rows >= MIN_SUPPORT else None


def levels_of(source: Any) -> dict[str, str | None]:
    """Category levels from an InputProduct or from a stored item's context."""
    if isinstance(source, dict):
        context = source.get("context") or {}
        return {
            "section": context.get("section"), "subcategory": context.get("subcategory"),
            "category": context.get("category"), "department": source.get("department"),
        }
    return {level: getattr(source, level, None) for level in LEVELS}


class CategoryProfile:
    def __init__(self) -> None:
        self._levels: dict[tuple[str, str], _Level] = defaultdict(_Level)
        self.rows = 0

    def add(self, levels: dict[str, str | None], unit: str, size: Decimal | None) -> None:
        self.rows += 1
        for level in LEVELS:
            name = levels.get(level)
            if not name:
                continue
            entry = self._levels[(level, name)]
            entry.units[unit] += 1
            if size is not None:
                entry.sizes[unit].append(size)

    @classmethod
    def from_validated(
        cls, rows: Iterable[tuple[dict[str, str | None], str, Decimal | None]],
    ) -> "CategoryProfile":
        profile = cls()
        for levels, unit, size in rows:
            profile.add(levels, unit, size)
        return profile

    @classmethod
    def from_items(cls, items: Iterable[dict[str, Any]]) -> "CategoryProfile":
        """From stored result items: Group A rows are the validated ones."""
        profile = cls()
        for item in items:
            if item.get("group") != "A":
                continue
            original = item.get("original") or {}
            unit = str(original.get("standard_uom") or "").strip().upper()
            try:
                size = Decimal(str(original.get("standard_size")))
            except (ArithmeticError, ValueError):
                size = None
            if unit:
                profile.add(levels_of(item), unit, size)
        return profile

    def view(self, levels: dict[str, str | None], *, leave_out: str | None = None) -> CategoryView:
        """``leave_out`` removes one row with that unit, so a row never vouches for itself."""
        for level in LEVELS:
            name = levels.get(level)
            entry = self._levels.get((level, name)) if name else None
            if entry is None:
                continue
            units = dict(entry.units)
            if leave_out and units.get(leave_out):
                units[leave_out] -= 1
            total = sum(units.values())
            if total >= MIN_SUPPORT:
                return CategoryView(level, name, total, units)
        return CategoryView(None, None, 0, {})

    def size_band(self, levels: dict[str, str | None], unit: str) -> tuple[Decimal, Decimal] | None:
        """Normal size range (2nd-98th percentile) for ``unit`` in this product's category."""
        for level in LEVELS:
            name = levels.get(level)
            entry = self._levels.get((level, name)) if name else None
            values = sorted(entry.sizes.get(unit, ())) if entry else []
            if len(values) >= MIN_SUPPORT:
                low = values[int(len(values) * 0.02)]
                high = values[min(len(values) - 1, int(len(values) * 0.98))]
                return low, high
        return None

    def plausible(self, levels: dict[str, str | None], unit: str, size: Decimal) -> bool | None:
        """None when the category has too little data to judge."""
        band = self.size_band(levels, unit)
        if band is None:
            return None
        low, high = band
        return low / RANGE_MARGIN <= size <= high * RANGE_MARGIN

    def as_dict(self) -> dict[str, Any]:
        liquid, mixed = [], []
        for (level, name), entry in sorted(self._levels.items()):
            if level != "category":
                continue
            view = CategoryView(level, name, sum(entry.units.values()), dict(entry.units))
            if view.liquid:
                liquid.append(name)
            elif view.mixed:
                mixed.append(name)
        return {
            "version": CATEGORY_PROFILE_VERSION, "validated_rows": self.rows,
            "liquid_categories": liquid, "mixed_categories": mixed,
        }
