"""Website evidence: for a product the retailer sells online, the page's size text and the
pack photo's printed quantity, each compared with the value the workbook carries. Everything
is stored per item with its sources (page link, image link, date), so a row can be shown
with the evidence that checked it."""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from app.agents.pack_reader import PACK_READER_VERSION, image_type
from app.services.sample_test_service import _DESCRIPTION_FIELDS, base_row
from app.services.web_check_service import FOUND, SiteAmount, WellcomeClient, compare

logger = logging.getLogger(__name__)
WEB_EVIDENCE_VERSION = "web-evidence-v2"
_UNIT_FACTOR = {"GM": ("GM", 1), "KG": ("GM", 1000), "ML": ("ML", 1), "L": ("ML", 1000), "EA": ("EA", 1),
                "OZ": ("GM", Decimal("28.35")), "LB": ("GM", Decimal("453.6"))}


def product_hint(item: dict[str, Any]) -> str:
    """Brand and name with every number removed, so the reader can tell whether a photo shows
    this product without being told a size."""
    import re
    context = item.get("context") or {}
    text = " ".join(str(context.get(k) or "") for k in ("item_brand_eng", "item_desc_eng"))
    return re.sub(r"\s+", " ", re.sub(r"[\\/]?\d[\d.,]*\s*[A-Za-z']{0,3}\b", " ", text)).strip()


def photo_amount(reading: dict[str, Any]) -> SiteAmount | None:
    if not reading.get("legible") or not reading.get("size") or reading.get("uom") not in _UNIT_FACTOR:
        return None
    base, factor = _UNIT_FACTOR[reading["uom"]]
    try:  # the reader may give a range or a fraction ("1.5-2"): then there is no single amount
        count = Decimal(str(reading.get("total_count") or reading.get("count") or 1))
        size = Decimal(str(reading["size"]).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if size <= 0 or count <= 0:
        return None
    return SiteAmount(base, size * factor * count, count, reading.get("printed") or "")


def _as_site(amount: SiteAmount | None, printed: str | None) -> dict[str, Any]:
    """Dress a photo reading as a 'site' so the same comparison applies."""
    if amount is None:
        return {"status": "NOT_FOUND"}
    text = f"{amount.count} X {amount.total / amount.count:f}{amount.uom}" if amount.count > 1 else f"{amount.total:f}{amount.uom}"
    return {"status": FOUND, "spec": text, "title": printed}


def verdict(text: dict[str, Any], photo: dict[str, Any]) -> tuple[str, str]:
    """One outcome for the row from the two readings, and a comment for the table."""
    answers = {text.get("answer"), photo.get("answer")}
    if (text.get("answer") == "YES" and photo.get("answer") == "YES"
            and text.get("matched_value") and photo.get("matched_value")
            and text.get("matched_value") != photo.get("matched_value")):
        return "SOURCE_CONFLICT", "The page text and pack photo support different packaging levels; a person should review them."
    if "YES" in answers and "NO" in answers:
        return "SOURCE_CONFLICT", "The page text and pack photo disagree with each other; a person should review them."
    if "YES" in answers:
        who = "page text and pack photo" if text.get("answer") == "YES" and photo.get("answer") == "YES" else ("page text" if text.get("answer") == "YES" else "pack photo")
        return "CONFIRMED", f"Confirmed by the {who}."
    if "NO" in answers:
        if text.get("answer") == "NO" and photo.get("answer") == "NO":
            return "DISPUTED", "Both the page text and the pack photo disagree with our value."
        other = "the pack photo" if text.get("answer") == "NO" else "the page text"
        return "DISPUTED", f"{'The page text' if text.get('answer') == 'NO' else 'The pack photo'} disagrees with our value; {other} could not settle it."
    return "UNRESOLVED", "Neither the page text nor the pack photo could be compared with our value."


class WebEvidenceService:
    def __init__(self, reader: Any, web: WellcomeClient | None = None, concurrency: int = 4, store: Any = None):
        self.reader = reader
        self.store = store
        self.web = web or WellcomeClient(store=store)
        self.concurrency = concurrency

    def check_item(self, item: dict[str, Any], site: dict[str, Any] | None = None) -> dict[str, Any]:
        """Page text and pack photo for one item, compared with our value."""
        row = base_row(item)
        descriptions = [(item.get("context") or {}).get(f) for f in _DESCRIPTION_FIELDS]
        site = site or self.web.lookup(row["item_no"])
        if site.get("status") != FOUND and self.store is not None:
            known_site = self.store.web_lookup(row["item_no"]) or {}
            if known_site.get("status") == FOUND:
                site = known_site  # the page did not load this time; the stored lookup stands
        doc: dict[str, Any] = {"item_no": row["item_no"], "row_number": row["row_number"], "version": WEB_EVIDENCE_VERSION,
                               "checked_at": datetime.now(timezone.utc), "status": site.get("status"),
                               "source": {"page_url": site.get("url"), "image_urls": site.get("images") or [], "site": "wellcome.com.hk"},
                               "site_text": site.get("spec") or site.get("title"),
                               "group": row["group"], "final": row["final"], "suggestion": row["suggestion"], "excel": row["excel"]}
        if site.get("status") != FOUND:
            doc.update(text={"answer": None, "reason": "Not sold on the website"}, photo={"answer": None, "reason": "No page"},
                       outcome="NOT_ONLINE", comment="This product is not on wellcome.com.hk.")
            return doc
        doc["text"] = compare(row["group"], row["final"], row["suggestion"], row["excel"], site, descriptions)
        images = site.get("images") or []
        known = self.store.web_lookup(row["item_no"]) if self.store is not None else None
        stored = (known or {}).get("photo_reading")
        if (stored and (known or {}).get("images") == images and stored.get("legible") is not None
                and stored.get("version") == PACK_READER_VERSION and stored.get("images_read") == len(images)):
            reading, error = stored, None  # the same photos were read before: reuse the reading
        else:
            reading, error = self._read_photos(images, product_hint(item))
            if self.store is not None and not error and reading:
                self.store.save_web_lookup(row["item_no"], {"photo_reading": reading})
        doc["photo_reading"] = reading
        doc["reused_reading"] = bool(stored and reading is stored)
        if error:
            doc["photo"] = {"answer": None, "reason": f"The photo could not be read: {error}"}
        elif not reading.get("legible"):
            doc["photo"] = {"answer": None, "reason": f"Pack text not legible: {reading.get('note') or 'no printed quantity visible'}"}
        else:
            amount = photo_amount(reading)
            if amount is None:
                doc["photo"] = {"answer": "NOT_COMPARABLE", "kind": None, "note": "",
                                "reason": f"Pack shows '{reading.get('printed')}', which is not a single amount"}
            else:
                doc["photo"] = compare(row["group"], row["final"], row["suggestion"], row["excel"], _as_site(amount, reading.get("printed")), descriptions)
                doc["photo"]["reason"] = doc["photo"]["reason"].replace("Site shows", "Pack shows", 1)
        doc["outcome"], doc["comment"] = verdict(doc["text"], doc["photo"])
        return doc

    def _read_photos(self, urls: list[str], product: str = "") -> tuple[dict[str, Any], str | None]:
        images: list[tuple[bytes, str]] = []
        for url in urls:
            try:
                data, declared = self.web.fetch_bytes(url)
                images.append((data, image_type(data, declared)))
            except Exception as exc:  # noqa: BLE001
                return {}, f"{type(exc).__name__}: {str(exc)[:120]}"
        if not images:
            return {"legible": False, "note": "The page has no product photo"}, None
        try:
            response = asyncio.run(self.reader.read(images, product))
        except Exception as exc:  # noqa: BLE001
            return {}, f"{type(exc).__name__}: {str(exc)[:160]}"
        return {**response.result.model_dump(), "model_id": response.model_id, "version": response.version,
                "images_read": len(images), "tokens": {"input": response.input_tokens, "output": response.output_tokens}}, None

    def run(self, items: list[dict[str, Any]], sites: dict[str, dict[str, Any]] | None = None,
            on_done: Callable[[int, dict[str, Any]], None] = lambda n, d: None) -> list[dict[str, Any]]:
        """Several products at a time: each check is a few slow page and model calls."""
        done = 0
        lock = Lock()
        out: list[dict[str, Any]] = [None] * len(items)  # type: ignore[list-item]

        def one(index: int) -> None:
            nonlocal done
            item = items[index]
            try:
                doc = self.check_item(item, (sites or {}).get(str(item.get("item_no") or "")))
            except Exception as exc:  # noqa: BLE001 - one product never stops the run
                logger.warning("website check failed for item %s: %s", item.get("item_no"), exc)
                doc = None
            out[index] = doc
            with lock:
                done += 1
                if doc is not None:
                    on_done(done, doc)

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            list(pool.map(one, range(len(items))))
        return out


# The status is a verdict on our value, worded for a reader of the sheet.
MATCHES, DIFFERS, UNSETTLED = "MATCHES", "DIFFERS", "UNSETTLED"
STATUS_LABELS = {MATCHES: "Matches website", DIFFERS: "Differs from website", UNSETTLED: "Website cannot settle it"}


def _amount_text(v: dict[str, Any] | None) -> str:
    if not v or not v.get("standard_size"):
        return "blank"
    pack = v.get("standard_pack_size")
    return f"{v['standard_size']} {v.get('standard_uom') or ''}".strip() + (f" × {pack}" if pack and str(pack) != "1" else "")


def evaluate(doc: dict[str, Any]) -> dict[str, Any]:
    """``status`` (MATCHES / DIFFERS / UNSETTLED), its label, one cell per source with the
    quote and its verdict, and a Why sentence quoting both sides."""
    text, photo, reading = doc.get("text") or {}, doc.get("photo") or {}, doc.get("photo_reading") or {}
    ours = _amount_text(doc.get("suggestion") if doc.get("group") == "C" and doc.get("suggestion") else doc.get("final"))
    site_text = doc.get("site_text") or ""
    printed = reading.get("printed") if reading.get("legible") else None
    word = {"YES": "matches ours", "NO": "differs from ours", "NOT_COMPARABLE": "cannot compare (another kind of unit)"}

    def result_word(result: dict[str, Any]) -> str:
        target = {"SUGGESTION": "supports the proposal", "EXCEL": "supports uploaded Excel",
                  "NEW_VALUE": "supplies a new value"}.get(result.get("matched_value"))
        return target or word.get(result.get("answer"), "cannot read this size")

    page_cell = f"{site_text} · {result_word(text)}" if site_text else "no size on the page"
    total = reading.get("images_read") or len((doc.get("source") or {}).get("image_urls") or [])
    which = f" (photo {reading['photo']} of {total})" if reading.get("photo") and total else (f" ({total} photos read)" if total else "")
    photo_cell = (f"{printed} · {result_word(photo)}{which}" if printed
                  else (f"not legible{which}" if reading else "no photo"))
    a, b = text.get("answer"), photo.get("answer")
    different_yes_targets = (
        a == "YES" and b == "YES" and text.get("matched_value") and photo.get("matched_value")
        and text.get("matched_value") != photo.get("matched_value")
    )
    if different_yes_targets:
        status = UNSETTLED
        why = (f"Our proposed value {ours}; the page text supports the proposal while the pack photo supports "
               "a different packaging level. A person must choose which level K/M represents.")
    elif "YES" in (a, b) and "NO" not in (a, b):
        status = MATCHES
        by = "page text and pack photo" if a == "YES" and b == "YES" else ("page text" if a == "YES" else "pack photo")
        shown = site_text if a == "YES" else printed
        why = (f"Excel holds no size; the {by} {'give' if ' and ' in by else 'gives'} {shown}, for the person to enter." if ours == "blank"
               else f"Our value {ours}; the {by} {'show' if ' and ' in by else 'shows'} the same size.")
        if doc.get("group") == "C" and ours != "blank":
            why += " The row was raised for a person; " + ("the site confirms the suggested value." if doc.get("suggestion") else "the site sides with the original value, so the raise was cautious.")
    elif "NO" in (a, b) and "YES" not in (a, b):
        status = DIFFERS
        src = "page text" if a == "NO" else "pack photo"
        shown = site_text if a == "NO" else printed
        kind = (text if a == "NO" else photo).get("kind")
        why = f"Our value {ours}; the {src} shows {shown}. " + (
            "The descriptions held the clue and the tool did not act on it." if kind == "TOOL_MISSED"
            else "The file agreed with itself, so nothing in it could show this." if kind == "DATA_MISLED" else "")
    elif "YES" in (a, b) and "NO" in (a, b):
        status = UNSETTLED
        why = f"Our value {ours}; page text {site_text} and pack photo {printed} disagree with each other. A person should look."
    else:
        status = UNSETTLED
        reason = ("the site counts pieces where our value is a weight, or the reverse" if "NOT_COMPARABLE" in (a, b)
                  else "the pack text is not legible" if reading and not reading.get("legible") else "the page shows no size")
        why = f"Our value {ours}; {reason}. Nobody is shown right or wrong."
    dropped = reading.get("other_product_photos") or []
    if dropped:
        why = why.strip() + f" Photo {', '.join(str(n) for n in dropped)} shows a different product and was not used."
    urls = (doc.get("source") or {}).get("image_urls") or []
    index = reading.get("photo")
    evidence_photo = urls[index - 1] if index and 0 < index <= len(urls) else (urls[0] if urls else None)
    return {"status": status, "status_label": STATUS_LABELS[status], "page_cell": page_cell, "photo_cell": photo_cell,
            "why": why.strip(), "evidence_photo": evidence_photo}
