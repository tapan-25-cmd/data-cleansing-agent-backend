# How the accuracy is calculated

Release 1 · UoM data cleansing · run of 24 September 2026, v0.2 workbook

## The rule in three lines

- The tool works on 12,542 live rows and must get three columns right: size (K), unit (L), pack count (M).
- Each row ends in one group by what the tool did, and each group is scored on its own question.
- A value is judged against a **second source in the same file**. The existing values are never the answer key.

| Group | What happened | Rows | Question scored |
|---|---|---:|---|
| A · No change | nothing written | 11,745 | was keeping the values right? |
| B · Changed by the tool | K, L or M written | 502 | was the change right? |
| C · Raised for a person | nothing written, a person decides | 295 | was raising it right? |
| Purged | skipped | 758 | not scored |

## Second sources

| Source | What it is | Weight |
|---|---|---|
| Product description | item and web description, English and Chinese (brand fields excluded: their numbers are names, not sizes) | independent proof |
| Old size field | columns I and J, converted with the unit table | consistency only: on 10,573 rows it is the same entry copied |
| Category pattern | for an ounce: how 9 in 10 products of the category are measured | consistency only |
| None found | nothing in the file can confirm or deny | shown, not scored |

## Group A · No change · 11,745

| # | Rule | Counts as | Rows | Example item |
|---|---|---|---:|---|
| 1 | description states Excel's size or piece count | right, confirmed | 483 | 074369 |
| 2 | description states a different size, same kind of unit | wrong | 3 | 240929 |
| 3 | old size converts exactly to Excel's size | right, consistent | 11,038 | 013151 |
| 4 | old size = size × pack (whole pack) | right, consistent | 139 | 695841 |
| 5 | old size within label rounding (1 unit or 1 %) | right, consistent | 50 | 628776 |
| 6 | nothing to compare with | not scored | 32 | 013789 |

Reported as counts, not a percentage: 11,438 match the legacy size, 483 confirmed by the description, 3 wrong, 32 unchecked. Correctness of this group is settled by the Merchandising benchmark (below).

## Group B · Changed by the tool · 502

**Rule accuracy** (client measure A): new value = arithmetic conversion of the old size. **502 of 502.**

| Rule | Rows | Example item |
|---|---:|---|
| exact conversion | 392 | 127191 |
| rounding, both rules agree | 36 | 644898 |
| rounding, rules differ, rounded to nearest | 32 | 628719 |
| unit label only (G → GM) | 38 | 703181 |
| fluid ounce at US fl oz | 4 | 185611 |
| does not match | 0 | |

**Second-source check** (ours): **118 of 118 checked**, coverage 23.5 %.

| Rule | Counts as | Rows | Example item |
|---|---|---:|---|
| description states the new value | right, confirmed | 33 | 637660 |
| unit spelling only, old size agrees | right, consistent | 38 | 703181 |
| ounce read by the category's pattern | right, consistent | 47 | 824078 |
| description states a different size | wrong | 0 | |
| nothing to compare with | not scored | 384 | 628719 |

## Group C · Raised for a person · 295

**Flag precision** (client measure C): the raise is borne out by the row's own data. **295 of 295**, every row judged, every row carries a comment.

| Reason raised | Counts as | Rows | Example item |
|---|---|---:|---|
| old size and Excel disagree, description silent | right | 170 | 044305 |
| description and Excel disagree | right | 40 | 236638 |
| same total, different split | right | 16 | 550624 |
| old size differs, description supports Excel | right | 4 | 483677 |
| ounce could be weight or liquid | right | 4 | 662056 |
| count could be pack or contents | right | 4 | 246033 |
| converted value contradicted by text or pack | right | 2 | |
| no size written anywhere | right | 54 | 015511 |
| old unit in no table | right | 1 | 127605 |
| raised but the text shows Excel was right | wrong | 0 | |
| left blank but a size is written | wrong | 0 | |

## Coverage

| Group | Rows | Scored | Coverage | Right | Wrong |
|---|---:|---:|---:|---:|---:|
| A | 11,745 | 11,713 | 99.7 % | 11,710 | 3 |
| B | 502 | 502 rule / 118 second source | 100 % / 23.5 % | 502 / 118 | 0 |
| C | 295 | 295 | 100 % | 295 | 0 |

## The client's three measures

| Client's measure | Our group | Now | Target |
|---|---|---|---|
| A · Rule accuracy | B | 502 of 502 | 100 % once rounding and fluid-ounce rules are confirmed |
| B · Correctness | A + B | waiting for the Merchandising cleansing (12,247 rows) | 95 % on size and on unit, each; pack separate |
| C · Flag precision | C | 295 of 295 | reported separately, not in the 95 % |

## Rules behind the comparisons

- Standard units: GM, ML, EA only.
- Unit table: 1 KG = 1000 GM · 1 OZ = 28.35 GM · 1 fl oz = 29.57 ML · 1 LB = 453.59 GM · 1 LT = 1000 ML · 1 PC = 1 EA.
- Rounding: nearest whole number (open with Eric; 32 rows change under truncation).
- Label rounding allowance: within 1 unit or 1 %, only after a conversion; same-unit values and piece counts must match exactly.
- Ounce: weight by default, fluid when the row or category is liquid, raised when the category is mixed.
- Pack count: never guessed; no count anywhere = single item, pack 1, with a note.
- AI reader: only a size literally written, with the words quoted and checked. A grade, capacity or year is never a size.
- Any disagreement between sources is raised; the tool never picks a side.

## Open with the client

- Relabel "Already correct" as "Matches the legacy size".
- Confirm round-to-nearest and the fluid-ounce reading.
- Format and timing of the Merchandising cleansing, for per-field scoring.
- Review status on Group B rows: the value is already in the download, so "Pending" misleads.
