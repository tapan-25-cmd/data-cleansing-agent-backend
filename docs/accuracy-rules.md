# How we measure accuracy

Release 1 · UoM data cleansing · our method, per group. Coverage figures are from the v0.2 workbook to show how far each source reaches.

## The approach

- The tool writes or keeps three columns per row: size (K), unit (L), pack count (M).
- Every row ends in one group by what the tool did. Each group is measured on its own question.
- A value cannot prove itself, so it is checked against a **second source in the same file**. The existing values are never the answer key.
- Every group is reported two ways:
  - **With a second source**: the share of rows a second source confirms or contradicts. This is what the tool can prove on its own.
  - **Without a second source**: the rows nothing in the file can check. These need a person, or the Merchandising benchmark once it is in the Data Lake.

| Group | What the tool did | Question measured |
|---|---|---|
| A · No change | kept K, L, M as entered | was keeping them right? |
| B · Changed by the tool | wrote K, L or M | was the change right? |
| C · Raised for a person | wrote nothing, raised with a reason | was raising right? |

## The second sources

| Source | What it is | What it proves |
|---|---|---|
| Product description | item and web description, English and Chinese; brand fields excluded (their numbers are names, not sizes) | independent: written by a different process about the product |
| Old size field | columns I and J, converted with the unit table | consistency only: often the same entry copied into both places |
| Category pattern | for an ounce: how 9 in 10 products of the category are measured | consistency only |

## Group A · No change

**What we check.** K, L and M against the description first, then against the old size.

| Check, in order | Outcome | Coverage on v0.2 |
|---|---|---|
| description states Excel's size or count | right, confirmed | 483 (4 %) |
| description states a different size, same kind of unit | wrong | 3 |
| old size converts to Excel's size, the whole pack, or within label rounding | right, consistent | 11,227 (96 %) |
| nothing to compare with | unchecked | 32 |

**With a second source.** Independent proof exists for the 483 rows the description covers; the 3 contradicted rows count against. The 11,227 old-size matches show consistency, not correctness: 10,573 are the identical entry in both fields.

**Without a second source.** For 96 % of the group only the old size agrees. Whether these values are right is settled by the Merchandising benchmark, per field, or by sampled human review. We report this group as counts (matched the legacy size / confirmed by the description / contradicted / unchecked), not as a percentage.

## Group B · Changed by the tool

**What we write.** K and L converted from the old size with the unit table; a unit spelling fix; a pack count read from the text or set to 1 for a single item.

**Check 1, the arithmetic.** Every written value must equal the conversion of its source (1 KG → 1000 GM, 16 OZ → 454 GM). This is checked on every row and must be 100 %; any miss is a defect.

**Check 2, the second source.**

| Check | Outcome | Coverage on v0.2 |
|---|---|---|
| description states the new value | right, confirmed | 33 |
| unit spelling only, old size holds the same value | right, consistent | 38 |
| ounce read by the category's pattern | right, consistent | 47 |
| description states a different size | wrong | 0 |
| nothing to compare with | unchecked | 384 (76 %) |

**With a second source.** 118 of 502 changes (24 %) can be traced to something else in the file, and none is contradicted.

**Without a second source.** For 76 % the old size is the only size in the file: the arithmetic is right, but only the benchmark or a person can say the old size itself was right. Pack counts written as 1 (no count anywhere) are reported on their own, against the benchmark's pack column.

## Group C · Raised for a person

**What we raise.** "Needs your review" when sources disagree or a value is only suggested; "Could not determine" when no size is written anywhere or the old unit has no conversion.

**What we check.** That the raise is borne out by the row's own data; every raised row is judged, so coverage is 100 %.

| Reason raised | Outcome | Coverage on v0.2 |
|---|---|---|
| old size and Excel disagree, description silent | right | 170 |
| description and Excel disagree; same total, different split; count or ounce ambiguous | right | 64 |
| old size differs, description supports Excel | right, the answer is likely Excel | 4 |
| converted value contradicted by text or pack | right | 2 |
| no size written anywhere; old unit in no table | right | 55 |
| raised, but the text shows Excel was right | wrong (needs the AI reasoning pass) | 0 |
| left blank, but a size is written | wrong | 0 |

**With a second source.** The raise is right when the disagreement or the absence is real in the file. Accuracy = justified raises ÷ all raises.

**Without a second source.** Whether the *answer* to each raise is what the tool suggested needs a person or the benchmark: we then report how many raised rows the benchmark resolves, and how many suggestions it agrees with.

## Coverage of the second source

| Group | Rows | Second source covers | Needs a person or the benchmark |
|---|---:|---:|---:|
| A | 11,745 | 486 (4 %) | 11,259 (96 %) |
| B | 502 | 118 (24 %) | 384 (76 %) |
| C | 295 | 295 (100 %) | 0 for the raise; all for the answer |

## Rules behind the comparisons

- Standard units: GM, ML, EA only.
- Unit table: 1 KG = 1000 GM · 1 OZ = 28.35 GM · 1 fl oz = 29.57 ML · 1 LB = 453.59 GM · 1 LT = 1000 ML · 1 PC = 1 EA.
- Rounding: nearest whole number.
- Label rounding allowance: within 1 unit or 1 % after a conversion; same-unit values and piece counts must match exactly.
- Ounce: weight by default, fluid when the row or category is liquid, raised when the category is mixed.
- Pack count: never guessed; no count anywhere = single item, pack 1, with a note.
- AI reader: only a size literally written, with the words quoted and checked against the text.
- Any disagreement between sources is raised; the tool never picks a side.

## What the benchmark adds

When the Merchandising Team's cleansing is in the Data Lake, the same rows are scored per field (size, unit; pack separately) for Groups A and B, and for Group C we report how many raised rows it resolves. Until then, the "without a second source" share of each group is what human review has to cover.
