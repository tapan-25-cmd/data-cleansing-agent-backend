"""Website check: the retailer's own product page as an independent second source.

For a row we search wellcome.com.hk by our item number, open the product page, and accept it
only when the page's "Item code" is that same number. The page's specification field (規格),
or its title when there is none, gives the size as the shop sells it. The tool's values are
then compared with it on the total amount and the kind of unit.

The site lists only what is on sale online, so many rows are "not found"; those are left out
of every figure, never counted as right. Pages are fetched slowly, one at a time.
"""
from __future__ import annotations

import html as htmlmod
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

BASE = "https://www.wellcome.com.hk"
USER_AGENT = "Mozilla/5.0 (compatible; uom-sample-test/1.0)"

FOUND, NOT_FOUND, NOT_MATCHED, ERROR = "FOUND", "NOT_FOUND", "NOT_MATCHED", "ERROR"
MAX_PHOTOS = 8  # a product's own gallery; more than this is not read
MAX_DETAIL_CANDIDATES_PER_QUERY = 4
MAX_DETAIL_CANDIDATES_TOTAL = 12

# unit → (base unit, factor)
_UNITS = {
    "G": ("GM", 1), "GM": ("GM", 1), "GRAM": ("GM", 1), "GRAMS": ("GM", 1), "KG": ("GM", 1000),
    "ML": ("ML", 1), "L": ("ML", 1000), "LT": ("ML", 1000), "LTR": ("ML", 1000), "LITRE": ("ML", 1000), "LITER": ("ML", 1000),
    "PC": ("EA", 1), "PCS": ("EA", 1), "S": ("EA", 1), "'S": ("EA", 1), "EA": ("EA", 1), "PK": ("EA", 1), "PACK": ("EA", 1),
    "BAGS": ("EA", 1), "SHEETS": ("EA", 1), "ROLLS": ("EA", 1), "CAPS": ("EA", 1), "TABS": ("EA", 1),
}
_UNIT_RE = r"(KG|GM|GRAMS?|G|ML|LTR|LT|LITRE|LITER|L|PCS|PC|'S|S|EA|PK|PACK|BAGS|SHEETS|ROLLS|CAPS|TABS)"
_NUM = r"(\d+(?:\.\d+)?)"
_MULTI = re.compile(rf"{_NUM}\s*[X×*]\s*{_NUM}\s*{_UNIT_RE}\b", re.I)          # 50 X 120GM
_MULTI_REV = re.compile(rf"{_NUM}\s*{_UNIT_RE}\s*[X×*]\s*{_NUM}\b", re.I)      # 120GM X 50
_SINGLE = re.compile(rf"{_NUM}\s*{_UNIT_RE}(?![A-Z])", re.I)


@dataclass
class SiteAmount:
    uom: str            # GM, ML or EA
    total: Decimal      # the whole pack in the base unit
    count: Decimal      # pieces, when the page gives them
    text: str


def read_amount(text: str | None) -> SiteAmount | None:
    """The amount a shop's size text states: '50 X 120GM', '6PC', '1.5L', '120GM X 2'."""
    if not text:
        return None
    t = text.upper().replace("＇", "'")
    m = _MULTI.search(t)
    if m:
        count, size, unit = Decimal(m.group(1)), Decimal(m.group(2)), m.group(3)
        base, factor = _UNITS[unit.upper()]
        return SiteAmount(base, count * size * factor, count, m.group(0))
    m = _MULTI_REV.search(t)
    if m:
        size, unit, count = Decimal(m.group(1)), m.group(2), Decimal(m.group(3))
        base, factor = _UNITS[unit.upper()]
        return SiteAmount(base, count * size * factor, count, m.group(0))
    # A weight or volume beats a piece count when both appear ("6PC 300G" is 300 GM of 6 pieces).
    found = [(Decimal(n), u.upper(), f"{n}{u}") for n, u in _SINGLE.findall(t)]
    measures = [f for f in found if _UNITS[f[1]][0] != "EA"]
    pieces = [f for f in found if _UNITS[f[1]][0] == "EA"]
    if measures:
        n, u, raw = measures[-1]
        base, factor = _UNITS[u]
        return SiteAmount(base, n * factor, pieces[-1][0] if pieces else Decimal(1), raw)
    if pieces:
        n, u, raw = pieces[-1]
        return SiteAmount("EA", n, n, raw)
    return None


def parse_page(page: str) -> dict[str, str | None]:
    """Title, specification (規格) and item code from a product page."""
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    parts = [htmlmod.unescape(x).strip() for x in re.split(r"<[^>]+>", body)]
    parts = [x for x in parts if x]
    title = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
    spec = next((parts[i + 1] for i, x in enumerate(parts[:-1]) if x in ("規格", "Specification", "Specifications")), None)
    code = next((m.group(1) for x in parts for m in [re.search(r"Item code\s*[:：]?\s*(\d{4,})", x)] if m), None)
    # Product photos: the structured-data image first, then the gallery images, in page order.
    images = re.findall(r'"image":\s*"(https://img\.[^"]+)"', page) + re.findall(r'<img[^>]+src="(https://img\.[^"]+)"[^>]*class="s-img"', page)
    images = list(dict.fromkeys(images))[:MAX_PHOTOS]
    return {"title": htmlmod.unescape(title.group(1)).strip() if title else None, "spec": spec, "item_code": code, "images": images}


def product_links(page: str) -> list[str]:
    """Product links exposed by Wellcome search, in any supported site language."""
    page = page.replace("\\/", "/")
    return list(dict.fromkeys(re.findall(
        r'/(?:en|zh-hant|zh-hans)/[^"\'<>\s]*?/i/\d+\.html', page, re.I,
    )))


def detail_queries(context: dict[str, Any]) -> list[str]:
    """Deterministic discovery queries. These find candidates; item code proves identity."""
    brand_en = str(context.get("item_brand_eng") or "").strip()
    brand_local = str(context.get("item_brand_local_lang") or "").strip()
    desc_en = str(context.get("item_desc_eng") or "").strip()
    desc_local = str(context.get("item_desc_local_lang") or "").strip()
    web_en = str(context.get("web_description_eng") or "").strip()
    web_local = str(context.get("web_description_chi") or "").strip()
    candidates = [
        " ".join(x for x in (brand_en, desc_en) if x),
        " ".join(x for x in (brand_local, desc_local) if x),
        web_en, web_local, desc_en, desc_local,
    ]
    return list(dict.fromkeys(q for q in candidates if len(q) >= 3))


def rank_product_links(links: list[str], query: str) -> list[str]:
    """Put URL slugs sharing query terms first and discard default-listing noise."""
    terms = {t.lower() for t in re.findall(r"[\w\u3400-\u9fff]+", query) if len(t) >= 2}
    ranked: list[tuple[int, int, str]] = []
    for position, link in enumerate(links):
        slug = urllib.parse.unquote(link).lower().replace("-", " ")
        score = sum(1 for term in terms if term in slug)
        if score:
            ranked.append((-score, position, link))
    return [link for _, _, link in sorted(ranked)]


class WellcomeClient:
    """``store`` (optional) remembers lookups by item number across runs: anything found is
    reused for ``reuse_days``; a "not found" is retried after the same time."""

    def __init__(self, delay: float = 1.0, timeout: float = 20, store: Any = None, reuse_days: int = 30):
        self.delay = delay
        self.timeout = timeout
        self.store = store
        self.reuse_days = reuse_days
        self._last = 0.0

    def _get(self, url: str) -> str:
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", "replace")
        finally:
            self._last = time.monotonic()

    def lookup(self, item_no: str) -> dict[str, Any]:
        """Find the product page for our item number; a page is used only when its item code matches."""
        known = self.store.web_lookup(item_no) if self.store is not None else None
        if known and known.get("status") in (FOUND, NOT_FOUND) and self.reuse_days > 0 and _fresh(known.get("saved_at"), self.reuse_days):
            return {**known, "reused": True}
        result = self._lookup_web(item_no)
        if self.store is not None and result.get("status") in (FOUND, NOT_FOUND):
            self.store.save_web_lookup(item_no, {k: v for k, v in result.items() if k != "reused"})
        return result

    def _lookup_web(self, item_no: str) -> dict[str, Any]:
        checked_at = time.strftime("%Y-%m-%d %H:%M")
        try:
            results = self._get(f"{BASE}/en/search?keyword={urllib.parse.quote(item_no)}")
            links = product_links(results)
            # A search with no match shows the site's default listing (many products).
            if not links or len(links) > 3:
                return {"status": NOT_FOUND, "checked_at": checked_at}
            for link in links:
                page = parse_page(self._get(BASE + link))
                if page["item_code"] == item_no:
                    return {"status": FOUND, "url": BASE + link, **page, "checked_at": checked_at}
            return {"status": NOT_MATCHED, "checked_at": checked_at}
        except Exception as exc:  # noqa: BLE001 - one page never stops the test
            return {"status": ERROR, "error": f"{type(exc).__name__}: {str(exc)[:200]}", "checked_at": checked_at}

    def lookup_by_details(self, item_no: str, context: dict[str, Any]) -> dict[str, Any]:
        """Search Wellcome by workbook descriptions, then require an exact page item code.

        Descriptions and brands are never treated as identity evidence. They only discover
        candidate URLs; a candidate is accepted solely when its displayed item code equals
        ``item_no``.
        """
        checked_at = time.strftime("%Y-%m-%d %H:%M")
        queries = detail_queries(context)
        inspected: set[str] = set()
        try:
            for query in queries:
                results = self._get(f"{BASE}/en/search?keyword={urllib.parse.quote(query)}")
                links = rank_product_links(product_links(results), query)
                for link in links[:MAX_DETAIL_CANDIDATES_PER_QUERY]:
                    if len(inspected) >= MAX_DETAIL_CANDIDATES_TOTAL:
                        break
                    if link in inspected:
                        continue
                    inspected.add(link)
                    page = parse_page(self._get(BASE + link))
                    if page["item_code"] == item_no:
                        result = {
                            "status": FOUND, "url": BASE + link, **page,
                            "checked_at": checked_at, "discovery_method": "WELLCOME_DETAIL_SEARCH",
                            "matched_query": query, "queries_tried": queries,
                            "candidate_pages_checked": len(inspected),
                        }
                        if self.store is not None:
                            self.store.save_web_lookup(item_no, result)
                        return result
                if len(inspected) >= MAX_DETAIL_CANDIDATES_TOTAL:
                    break
            return {
                "status": NOT_FOUND, "checked_at": checked_at,
                "discovery_method": "WELLCOME_DETAIL_SEARCH", "queries_tried": queries,
                "candidate_pages_checked": len(inspected),
            }
        except Exception as exc:  # noqa: BLE001 - one product never stops a batch
            return {
                "status": ERROR, "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "checked_at": checked_at, "discovery_method": "WELLCOME_DETAIL_SEARCH",
                "queries_tried": queries, "candidate_pages_checked": len(inspected),
            }

    def fetch_bytes(self, url: str, limit: int = 6_000_000) -> tuple[bytes, str]:
        """An image from the shop's image host, with its media type."""
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read(limit), response.headers.get_content_type()
        finally:
            self._last = time.monotonic()



def _fresh(saved_at: Any, days: int) -> bool:
    from datetime import datetime, timedelta, timezone
    if not saved_at:
        return False
    if saved_at.tzinfo is None:
        saved_at = saved_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - saved_at < timedelta(days=days)


def _dec(v: object) -> Decimal | None:
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def tool_amount(values: dict[str, Any]) -> SiteAmount | None:
    size, uom, pack = _dec(values.get("standard_size")), str(values.get("standard_uom") or "").upper(), _dec(values.get("standard_pack_size")) or Decimal(1)
    if size is None or uom not in _UNITS:
        return None
    base, factor = _UNITS[uom]
    return SiteAmount(base, size * factor * pack, pack, "")


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= max(Decimal("0.01") * max(a, b), Decimal("0.5"))


def _say(a: SiteAmount) -> str:
    return f"{a.total.normalize():f} {a.uom}"


YES, NO, NOT_COMPARABLE = "YES", "NO", "NOT_COMPARABLE"
TOOL_MISSED, DATA_MISLED = "TOOL_MISSED", "DATA_MISLED"


def clue_in_text(amount: SiteAmount, texts: list[str | None]) -> bool:
    """Did the descriptions carry the site's number? A count ("30 X", "\\4", "6S", "3PC") or the
    size figure itself. If so the tool had the clue and missed it; if not, the file misled it."""
    text = " ".join(t for t in texts if t).upper()
    numbers = {amount.total, amount.count}
    if amount.count > 1:
        numbers.add(amount.total / amount.count)
    for num in numbers:
        n = f"{num.normalize():f}"
        if re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", text):
            return True
    return False


def _outcome(answer: str | None, reason: str, note: str, kind: str | None = None,
             matched_value: str | None = None) -> dict[str, Any]:
    return {"answer": answer, "reason": reason, "note": note, "kind": kind,
            "matched_value": matched_value}


def compare(group: str, values: dict[str, Any], suggestion: dict[str, Any] | None, excel: dict[str, Any], site: dict[str, Any],
            descriptions: list[str | None] | None = None) -> dict[str, Any]:
    """The website's answer for a row: ``answer`` YES, NO, NOT_COMPARABLE or None (no page),
    ``reason`` quoting the site, ``note`` a plain explanation, and on a NO the ``kind``:
    TOOL_MISSED (the descriptions held the clue) or DATA_MISLED (the file agreed with
    itself, so nothing in it could show the error).

    A and B: do the values the workbook carries match what the shop sells (same total amount,
    same kind of unit)? C: the row was raised for a person; it is NO only when the site
    disagrees with Excel and with the tool's suggestion, so the person would have neither."""
    descriptions = descriptions or []
    if site.get("status") != FOUND:
        reason = {NOT_FOUND: "Not sold on the website", NOT_MATCHED: "No page with this item code",
                  ERROR: "The website could not be read"}.get(site.get("status"), "No website answer")
        return _outcome(None, reason, "This product is not on wellcome.com.hk, so the site cannot check it.")
    shown = site.get("spec") or site.get("title")
    amount = read_amount(site.get("spec")) or read_amount(site.get("title"))
    if amount is None:
        return _outcome(None, f"Site shows '{shown}', no size in it", "The product page shows no size, so there is nothing to compare.")
    quote = f"Site shows '{shown}'"

    def agrees(v: dict[str, Any]) -> bool | None:
        ours = tool_amount(v)
        if ours is None:
            return None
        # The shop usually lists one unit's size ("200GM") even for a 4-pack, so a site figure
        # with no count of its own agrees with either our total or our unit size.
        unit = ours.total / ours.count if ours.count else ours.total
        matches = _close(ours.total, amount.total) or (amount.count == 1 and _close(unit, amount.total))
        if ours.uom != amount.uom:
            # Pieces against a weight cannot be compared. Grams against millilitres with the
            # same number is a unit question, not a wrong size; a different number is wrong.
            if "EA" in (ours.uom, amount.uom) or matches:
                return None
            return False
        return matches

    not_comparable = (f"The site measures this in {'pieces' if amount.uom == 'EA' else 'weight or volume'} and the file in "
                      f"another kind of unit. Both can be true (10 dumplings and 180 GM), so this row is left out of the figure.")
    if group == "C":
        if suggestion is not None and agrees(suggestion):
            return _outcome(YES, f"{quote}; matches the suggestion {_say(tool_amount(suggestion))}",
                            "The tool raised this row and suggested the value the site confirms. The person only has to accept it.",
                            matched_value="SUGGESTION")
        on_excel = agrees(excel)
        if on_excel is None and tool_amount(excel) is None:
            return _outcome(YES, f"{quote}; Excel is blank, the site gives {_say(amount)}",
                            "The row was raised because Excel holds no size. The site supplies the value for the person to enter.",
                            matched_value="NEW_VALUE")
        if on_excel is None:
            return _outcome(NOT_COMPARABLE, f"{quote}; Excel has no comparable size", not_comparable)
        if on_excel:
            return _outcome(YES, f"{quote}; this confirms Excel ({_say(tool_amount(excel))})",
                            "The tool raised this row because a description disagreed with Excel. The site sides with Excel, "
                            "so the raise was cautious: nothing was changed and a person keeps Excel.",
                            matched_value="EXCEL")
        return _outcome(NO, f"{quote} = {_say(amount)}; Excel has {_say(tool_amount(excel))} and the suggestion differs too",
                        "The site disagrees with Excel and with the tool's suggestion, so the person reviewing this row would "
                        "have neither right answer in front of them.", TOOL_MISSED)
    ok = agrees(values)
    ours = tool_amount(values)
    if ok is None:
        return _outcome(NOT_COMPARABLE, f"{quote}; not comparable with {_say(ours) if ours else 'a blank size'}", not_comparable)
    if ok:
        return _outcome(YES, f"{quote} = {_say(amount)}; ours {_say(ours)}",
                        "The value the workbook carries is the size the shop sells.")
    if clue_in_text(amount, descriptions):
        return _outcome(NO, f"{quote} = {_say(amount)}; ours {_say(ours)}",
                        "The descriptions carried the site's number and the tool did not act on it. This is a gap in the tool.",
                        TOOL_MISSED)
    return _outcome(NO, f"{quote} = {_say(amount)}; ours {_say(ours)}",
                    "Excel, the old size field and the descriptions all agreed with each other, so nothing in the file "
                    "could show this error. Only a second source, like this site, reveals it.", DATA_MISLED)
