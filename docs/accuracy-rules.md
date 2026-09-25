# How the accuracy is calculated

Release 1 · UoM data cleansing · figures from the run of 24 September 2026 (v0.2 workbook)

## In one page

- The workbook has 13,300 product rows in the two Release 1 departments. 758 are purged and skipped. The tool works on the other **12,542**.
- The tool's job is to get three columns right for each product: **size (K)**, **unit (L)** and **pack count (M)**. It reads the old size field (columns I and J) and the product's own descriptions.
- Every row ends in one of three groups, decided by what the tool did:
  - **Group A · No change** — nothing written (11,745 rows)
  - **Group B · Changed by the tool** — K, L or M written (502 rows)
  - **Group C · Raised for a person** — nothing written, a person decides (295 rows)
- Accuracy is measured **per group**, because each group answers a different question:
  - A: was keeping the values right?
  - B: was the change right?
  - C: was raising it right?
- A value is judged against a **second source in the same file**: the old size field, the product's description, or the category's usual unit. **The existing values are never the answer key.** The client asked for this rule, and it is how the tool already works.
- The client's three Release 1 measures map onto the groups: rule accuracy → Group B, flag precision → Group C, and correctness against the Merchandising Team's cleansing → Groups A and B, once that data is in the Data Lake.

## What "second source" means

A row's own value cannot prove itself. The tool looks for something else in the file that agrees or disagrees with it.

| Second source | What it is | Strength |
|---|---|---|
| Product description | The item description and the web description, each in English and Chinese. Brand fields are not used, because brand names carry numbers that are not sizes (7-UP, 3-in-1). | Strongest: written by a different process about the product itself |
| Old size field | Columns I and J, the size as the old system stored it, converted with the unit table | Weaker: on 10,573 rows it is the same entry copied into both places |
| Category pattern | For an ounce only: how nine in ten complete products of the same category are measured (weight or liquid) | Used once, to read an ounce |
| None found | Nothing in the file can confirm or deny the value | The row is shown and left out of the score |

## Group A · No change · 11,745 rows

**Question:** was keeping the values right?

The tool read K, L and M and wrote nothing. Every row is placed in exactly one line below, the first that fits.

| # | Rule | Counts as | Rows | Example |
|---|---|---|---:|---|
| 1 | The description states the same size or piece count Excel has | right, confirmed | 483 | Birthday candle: Excel 13 EA; the Chinese web description says 13支 (13 pieces) |
| 2 | The description states a different size in the same kind of unit | wrong | 3 | Instant noodle case: Excel 450 GM × 6; the web description says 6 × 90GM |
| 3 | The old size converts to exactly Excel's size | right, consistent | 11,038 | Cake mix: old size 250 GM, Excel 250 GM (10,573 of these are the identical entry) |
| 4 | The old size equals size × pack, the whole pack | right, consistent | 139 | E-fu noodles: old size 160 GM, Excel 80 GM × 2 |
| 5 | The old size agrees within label rounding (1 unit or 1 %) | right, consistent | 50 | Muffin mix: old size 17.1 OZ = 485 GM, Excel 484 GM |
| 6 | Nothing to compare with | not scored | 32 | Rice paper: old unit "ST" has no conversion, no size in the text |

**Result:** 11,710 right of 11,713 scored (99.97 %); coverage 99.7 %.

**How to read it:** only the 483 in line 1 are confirmed by something independent of the entry. The 11,227 in lines 3 to 5 show the two records are consistent, which rules out a typo but not a shared mistake. Following the client's point, the tab now reports this group as counts ("11,438 match the legacy size, 483 confirmed by the description") and not as a percentage. The real test of Group A is the Merchandising benchmark below.

## Group B · Changed by the tool · 502 rows

**Question:** was the change right?

The tool wrote K, L or M itself, mostly by converting the old size with the unit table (1 KG → 1000 GM, 16 OZ → 454 GM).

**Rule accuracy** (the client's measure A): does the new value equal the arithmetic conversion of the old size?

| Rule | Rows | Example |
|---|---:|---|
| Exact conversion | 392 | Bread flour: 1 KG → 1000 GM |
| Needs rounding, both rounding rules give the same number | 36 | Muffin mix: 16.9 OZ = 479.11 → 479 GM |
| Needs rounding, the rules differ; the tool rounds to nearest | 32 | Brownie mix: 16 OZ = 453.59 → 454 GM (truncation would give 453) |
| Only the unit label changed, size unchanged | 38 | Peppercorn: 200 G → 200 GM |
| Fluid ounce, converted at US fluid ounces | 4 | Corn syrup: 16 FZ → 473 ML |
| Does not match the conversion | 0 | |

**Result:** 502 of 502 (100 %). Target 100 % once the rounding rule (round to nearest) and the fluid-ounce rule are confirmed with Eric; 32 rows change if truncation is chosen.

**Second-source check** (our own): what in the file agrees with the new value?

| Rule | Counts as | Rows | Example |
|---|---|---:|---|
| The description states the new value | right, confirmed | 33 | Abalone soup: 1.8 KG → 1800 GM; the Chinese web description says 1800克 |
| Only the unit spelling changed; the old size holds the same value | right, consistent | 38 | Peppercorn: 200 G → 200 GM, old size 200 GM |
| Ounce read as weight or liquid by the category's pattern | right, consistent | 47 | Minced clams: 6.5 OZ → 184 GM; 300 of 329 complete products in the category use GM |
| The description states a different size | wrong | 0 | |
| Nothing to compare with | not scored | 384 | Brownie mix: 16 OZ → 454 GM; the descriptions give no size |

**Result:** 118 right of 118 scored (100 %); coverage 23.5 %. For 384 rows the old size is the only size in the file, so the arithmetic is right by the table but nothing else confirms it. That is what the Merchandising benchmark will settle.

## Group C · Raised for a person · 295 rows

**Question:** was raising it right?

The tool wrote nothing and gave the row to a person with a reason. A raise is right when the row's own data bears it out. Every raised row is judged, so coverage is 100 %.

| Rule | Counts as | Rows | Example |
|---|---|---:|---|
| The old size and Excel disagree, and the description settles nothing | right | 170 | Muffin mix: old size 12.3 OZ = 349 GM, Excel 375 GM; suggested 349 GM |
| The description and Excel disagree (size, count, or the two languages) | right | 40 | Mackerel case: Excel 120 GM × 50; the web description says "case 25 × 120GM" |
| Same total, different split | right | 16 | Shrimp noodles: old size 55 GM, Excel 550 GM × 1, description "\10"; suggested 55 GM × 10 |
| The old size differs but the description supports Excel | right | 4 | Sweetener: old size 50 PC, Excel 1 GM; the description says "1G" |
| An ounce could be weight or liquid (mixed category) | right | 4 | Sweet gherkins: 16 OZ, category holds both |
| A count could be the pack or its contents | right | 4 | Abalone noodle coupon: "12PCS" |
| The converted value is contradicted by the text or the pack | right | 2 | |
| No size is written anywhere | right | 54 | Green tea: no old size, no size in any description |
| The old unit is in no table | right | 1 | Waffle cone voucher: unit "ST" |
| Raised, but the text shows Excel was right | wrong | 0 | (found by the AI reasoning trial; not yet run on this workbook version) |
| Left blank, but a size is written in the description | wrong | 0 | |

**Result:** 295 of 295 (100 %). This is the client's **flag precision** measure. Every flagged row carries a comment, and 64 carry a proposed value.

## Coverage, in one table

| Group | Rows | Scored | Coverage | Right | Wrong | Figure shown |
|---|---:|---:|---:|---:|---:|---|
| A · No change | 11,745 | 11,713 | 99.7 % | 11,710 | 3 | counts, not a percentage |
| B · Changed by the tool | 502 | 502 (rule) / 118 (second source) | 100 % / 23.5 % | 502 / 118 | 0 / 0 | 100 % rule accuracy |
| C · Raised for a person | 295 | 295 | 100 % | 295 | 0 | 100 % flag precision |

Rows that cannot be checked are always shown and never counted as right.

## The client's three measures, side by side

| Client's measure | Our group | Rule | Now | Target |
|---|---|---|---|---|
| A · Rule accuracy | B | new value = arithmetic conversion of the old size | 502 of 502 | 100 % once rounding and fluid-ounce rules are confirmed |
| B · Correctness | A + B | per field, against the Merchandising Team's cleansing in the Data Lake | waiting for the benchmark (12,247 rows) | 95 % on size and on unit, each on its own; pack reported separately |
| C · Flag precision | C | the flag is justified by the row's own fields | 295 of 295 | reported separately, not in the 95 % |

## Two tests with the answer hidden

Alongside the per-group figures, two blind tests check the tool's parts on known answers:

| Test | What is hidden | Result |
|---|---|---|
| Unit table | The team's own conversions of 1,112 products | 979 reproduced exactly; the differences are listed with both values |
| Checker | Deliberate errors planted in 7,955 correct rows | 7,701 caught; a pack count off by one is the weak spot (21.6 % caught) |

The description reader has a third test (sizes hidden from the reader on real descriptions) that costs one AI call per product and is run on request.

## Rules that decide a comparison

- **Standard units:** GM for weight, ML for volume, EA for pieces. Nothing else is written.
- **Unit table:** twelve conversions, including Chinese spellings; 1 KG = 1000 GM, 1 OZ = 28.35 GM, 1 fl oz = 29.57 ML, 1 LB = 453.59 GM, 1 LT = 1000 ML, 1 PC = 1 EA.
- **Rounding:** to the nearest whole number, as Excel rounds. Open with Eric.
- **Label rounding allowance:** a converted value counts as the same when it is within 1 unit or 1 %, whichever is larger. Only after a conversion; same-unit values and piece counts must match exactly.
- **Ounce:** weight by default; a fluid ounce when the row or the category is liquid; raised when the category is mixed.
- **Pack count:** never guessed. A product with no count written anywhere is a single item, pack 1, with a note.
- **Description reader (AI):** may only report a size that is literally written; every answer must quote the words, and the quote is checked against the text. A number that is a grade, a capacity or a year is never a size.
- **Discrepancies:** whenever two sources disagree the row is raised; the tool never picks a side.

## What is open with the client

1. Relabel "Already correct" as "Matches the legacy size" (agreed in principle; changes the screen and the export).
2. Confirm round-to-nearest and the fluid-ounce reading (32 + 4 rows depend on it).
3. The format and timing of the Merchandising Team's cleansing, so correctness can be scored per field.
4. Review status on Group B rows: the value is already in the download, so "Pending" reads as "not applied".
