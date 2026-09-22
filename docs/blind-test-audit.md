# Blind-Test Audit — Where the Agent Is Wrong, and Why

**Date:** 21 September 2026 · **Workbook:** `20260908_UoM_Snapshot_v0.2.xlsx` · **Status:** findings only; nothing in this document has been changed in the code.

Every blind-test disagreement was checked against the full workbook row (descriptions,
legacy data, entered values, category). All 422 unit-conversion rows, all 30 pack-size rows,
and all 18 scored AI differences were reviewed, not only the 25 examples the page shows.

Each pattern is marked with who is wrong:
**Agent** = the agent's logic is wrong and can be fixed ·
**Data** = the workbook is wrong and the agent is right ·
**Policy** = both are defensible and the business must choose.

## Summary

| # | Pattern | Rows | Who is wrong | Affects real results? |
|---|---|---:|---|---|
| 1 | A number on a container or tool is read as the product size | 4 | Agent (AI) | Only future blank rows |
| 2 | A number inside a product name is read as a size | 4 | Agent (AI) | Only future blank rows |
| 3 | AI picks the per-piece size when the text also states the pack total | 5 | Agent (AI) / Policy | Only future blank rows |
| 4 | AI reads the size of a bundle Excel records as a count | 4 | Policy | Only future blank rows |
| 5 | `OZ` on a liquid is a fluid ounce, converted as a weight | 15 | **Agent (rules)** | **Yes — about 7 of 74 conversions are clearly liquids** |
| 6 | Legacy holds the whole-pack total; Excel holds per-unit × pack | 145 | Agent (rules) / Policy | Low today (no affected row has pack > 1) |
| 7 | Legacy is a count of pieces; people entered a weight | 123 | Policy / missing data | **Yes — 132 blanks were filled with "N EA"** |
| 8 | Legacy is a weight; people entered a count | 38+ | Data / Policy | No |
| 9 | Same number, different unit (100 ML vs 100 GM) | 7 | Data | No |
| 10 | Excel was rounded to the label value (2268 vs 2270 GM) | 11 | Policy (tolerance) | No |
| 11 | Excel differs from a correct conversion with no explanation | 35 | Mostly Data | No |
| 12 | Case packs: Excel pack = case count × inner pack | 17 | Policy | No |
| 13 | A content count ("8 PACK" tortillas) is read as the sellable pack | 13 | Agent (rules) / Policy | Low (0 pack fills from this pattern in v0.2) |
| 14 | Local-language units could not be converted | 58 | Agent (rules) — **fixed** | Fixed in ruleset `poc-v2` |

## A. AI description reading (370 tested)

**1. Capacity or range of a container or tool — Agent wrong (4).**
The number describes the object, not an amount of product. Excel counts pieces.

| Item | Text | AI read | Excel |
|---|---|---|---|
| 102210 | 1L MICROWAVE BOX | 1000 ML | 10 EA |
| 105205 | KITCHEN SCALE 電子磅(3KG) | 3000 GM | 1 EA |
| 100933 | 5OZ PUDDING CUP 5安梅花杯連蓋 | 142 GM | 20 EA |
| 101006 | 8OZ PUDDING CUP | 227 GM | 20 EA |

All four are in Baking Aids and carry a container or tool word in the text itself
(`BOX`, `CUP`, `SCALE`, `杯`, `盒`, `磅`).

**2. A number that is part of the product name — Agent wrong (4).**

| Item | Text | AI read | Excel | What the number is |
|---|---|---|---|---|
| 485029 | 3.3G YOGHURT / 3.3乳酪 | 3.3 GM | 100 GM | 3.3 g protein: a product name |
| 476218 | MODENA BAL VINEGAR5G | 5 GM | 250 ML | a grade ("5 leaf/gold") |
| 705269 | ORG BSM 4L VINEGAR / 有機四葉香醋 | 4000 ML | 250 ML | "4L" = four-leaf (四葉) grade |
| 658997 | MACHHERONI 500KG | 500,000 GM | 500 GM | a typing error for 500GM |

Signals available in the text: the local-language name explains the token (`四葉` = four
leaf); the value is implausible for a retail product (500 kg of pasta, 3.3 g of yoghurt);
a weight on a liquid (vinegar "5G").

**3. Per-piece size chosen when the pack total is also stated — Agent wrong, policy decides (5).**

| Item | Text | AI read | Excel |
|---|---|---|---|
| 268045 | …CHEESE ORIG 180G18P / 180G(10GX18) | 10 GM | 180 GM × 1 |
| 682922 | 高湯包8G X 8P | 8 GM | 64 GM × 1 |
| 240929 | 5 CASE/6 X 90GM | 90 GM | 450 GM × 6 |
| 240572 | 4 CASE/16 X 200GM | 200 GM | 800 GM × 16 |
| 777144 | 意式肉丸意粉30GM | 30 GM | 300 GM (text typo) |

The prompt tells the AI to return the per-unit measurement, and it did. Whether the unit
size should be the piece or the sellable pack is the open "what do size and pack size
mean" decision. Item 268045 is the clearest case: the text states 180G twice and the AI
still returned the 10G sachet.

**4. Bundles recorded as a count — Policy (4).**
`500MLx2` twin pack, `320GX4PACKS`, `4X100ML`, `100ML X 4`: the AI read the size correctly;
Excel records `1 EA × 2/4`. People treat a bundle as one sellable item.

**One sensible decline:** 269217 `200ML+15ML` (two sizes) — the AI declined. Correct behaviour.

**14. Local-language units — fixed.** 58 of the 60 "no answers" were correct readings such as
`140克` that the rules could not convert. Ruleset `poc-v2` adds these spellings.
Re-testing those 60 products will show the real figure.

## B. Unit conversion from legacy data (422 of 11,937 differ)

**5. `OZ` on liquids is a fluid ounce — Agent wrong, and it affects real output.**
In the answer key, 78 `OZ` rows match the weight ounce, but **13 match the US fluid ounce
exactly** (×29.57): mayonnaise 15 OZ → 443 ML, Miracle Whip 30 OZ → 887 ML. The rule set
converts every `OZ` as a weight.

The agent really converted **74** `OZ` products this way. About 31 look like liquids or
semi-liquids; these are clearly liquids and clearly wrong as written:

| Item | Product | Legacy | Agent wrote | Fluid-ounce value |
|---|---|---|---|---|
| 576421 | OAT MILK | 48 OZ | 1361 GM | 1420 ML |
| 671891, 672071 | ALMOND MILK | 48 OZ | 1361 GM | 1420 ML |
| 633560, 633669 | KEFIR | 32 OZ | 907 GM | 946 ML |
| 215608, 215749 | FLAX OIL | 12 OZ | 340 GM | 355 ML |
| 137331 | DARK SYRUP | 22 OZ | 624 GM | 651 ML |

Sauces, yoghurts and soups are genuinely ambiguous (US labels use either).

**6. Legacy is the whole-pack total — Agent wrong when pack > 1 (145).**
Among 1,239 answer-key rows with pack > 1, legacy holds the per-unit size in 1,050 and the
**whole-pack total in 145** (noodles 350 GM → 70 GM × 5; pancakes 260 GM → 130 GM × 2).
The agent always writes the legacy value as the unit size, so for those 145 it would
write the total. 22 more differ by a count written in the text (`NDL\6`: 55 GM vs 330 GM).
No row the agent actually converted in v0.2 has pack > 1, so exposure today is low.

**7. Legacy counts pieces, people entered a weight — Policy / missing data (123).**
When legacy says `PC`, people kept a count 94% of the time in Grocery 2 but only **79% in
Dairy & Frozen**, where 105 rows hold a weight instead (frozen dumplings: legacy `10 PC`,
Excel `200 GM`). The weight is not in the legacy data, so the agent cannot produce it.
This matters because the agent **filled 132 blank Dairy & Frozen rows with "N EA" from
`PC`**; by this base rate roughly one in five of those would have been entered as a weight
by a person. Many are coupons and vouchers (`1 PC → 1 EA`), which are safe.

**8. Legacy is a weight, people entered a count (38+).** `APPLE PUREE 4P`: legacy 120 GM,
Excel `4 EA`. The entered value discards the size. The agent's answer is arguably better.

**9. Same number, different unit — Data (7).** Vanilla extract 100 ML vs 100 GM; pasta sauce
500 GM vs 500 ML. One side is a slip. The agent cannot tell which.

**10. Rounded to the label — Policy (11).** 5 LB and 80 OZ convert to 2268 GM; Excel says
2270 GM. 7.5 OZ → 213 vs 215. People entered the metric figure printed on the pack. A
tolerance of about 1% (instead of 1 unit) would treat these as agreement.

**11. Unexplained — mostly Data (35).** 16 OZ → 454 GM but Excel 907 GM (that is 32 OZ);
14.5 OZ → 411 GM but Excel 246 GM (drained weight?); 225 GM vs 255 GM (digit slip);
168 GM vs 68 GM (dropped digit). Two are confirmed against the agent's favour by nothing
and in its favour by text: 401174 (`3安士` = 85 GM, Excel 170 GM).

## C. Pack size from the description (30 of 144 differ)

**12. Case packs — Policy (17).** The text says `CASE 12 X 505GM`, Excel holds 24; `3S CASE
16 X 142GM` → 48 (16 × 3); `4'S CASE 24 X 32GM` → 96 (24 × 4); but also `CASE 24 X 340GM`
→ 12 and `3'S CASE 30 X 70GM` → 3. Excel is not consistent with itself, so no rule can
match it. This is the "case pack meaning" decision, with evidence.

**13. Content count read as the sellable pack — Agent wrong (13).** `TORTILLA (8 PACK)` 320 GM,
`16PK MINI CHEESE ROLLS` 480 GM, `4PACKS` microwave rice 480 GM: the weight is the whole
product and the count is what is inside it, so Excel's pack 1 is right. The agent's
`N PACK` / `NPK` pattern proposes the count. `BAKED BEANS 3PK (190GMX3)` is the opposite
case (a real multipack), which is why this needs the weight to decide: if the stated
weight is the total, the count is contents; if it is per piece, it is a pack.

## What would make the agent more intelligent (proposals, not yet built)

1. **AI instructions:** decline when the measurement sits on a container, tool or appliance
   word; decline when the local-language name explains the token as a name or grade;
   prefer the stated pack total over a per-piece size inside brackets or after `X`.
2. **Plausibility guard (deterministic):** flag a proposed size for review when it is far
   outside the range of its own category (500 kg pasta, 3.3 g yoghurt, 4 L vinegar).
   Category ranges come from the workbook's own validated rows.
3. **Fluid ounces:** treat `OZ` as ambiguous for liquid categories (milk, oil, drinks,
   syrup): send to review with both candidates instead of writing a weight. Needs O-2.
4. **Whole-pack totals:** when legacy ÷ pack (or ÷ a count in the text) is a clean value,
   raise it as a candidate instead of silently writing the total.
5. **Pack versus contents:** only propose a pack from `N PACK`/`NPK` when the text also
   states a per-piece size; otherwise treat it as contents.
6. **Pieces in Dairy & Frozen:** fill `N EA` from `PC` automatically only for categories
   where people consistently keep counts; send the rest to review.
7. **Tolerance:** about 1% for converted imperial units, pending business approval.
