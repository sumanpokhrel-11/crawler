"""Local ingest server for the Chrome extension lane.

The extension posts RAW extracted fields; this normalises them with the same
normalize.py the HTTP lane uses, so both lanes emit identical record shapes.

    python3 scrapers/collector.py        # listens on 127.0.0.1:8765
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import normalize as N

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "extension"
OUT = ROOT / "data" / "out"
OUT.mkdir(parents=True, exist_ok=True)
RAW.mkdir(parents=True, exist_ok=True)

TARGETS = OUT / "review_targets.json"
STATE = OUT / "review_sweep_state.json"
LIST_TARGETS = OUT / "listing_targets.json"
LIST_STATE = OUT / "listing_state.json"

_lock = threading.Lock()
_seen_products: set[str] = set()
_seen_reviews: set[str] = set()
_seen_offers: set[tuple] = set()
STATS = {"products": 0, "offers": 0, "reviews": 0, "batches": 0, "skipped": 0}


# Canonical identity for every source, so a slug can never leak into a display
# name (or vice versa) if an adapter passes its arguments the wrong way round.
SOURCES = {
    "dan_murphys": "Dan Murphy's",
    "bws": "BWS",
    "liquorland": "Liquorland",
    "thirsty_camel": "Thirsty Camel",
    "nicks": "Nicks Wine Merchants",
    "mybottleshop": "MyBottleShop",
    "liquor_loot": "Liquor Loot",
    "kent_street_cellars": "Kent Street Cellars",
}
_DISPLAY_TO_SLUG = {v.lower(): k for k, v in SOURCES.items()}


def canon_source(source, retailer):
    """Return (slug, display_name) regardless of which way round they arrived."""
    s = (source or "").strip()
    r = (retailer or "").strip()
    if s.lower() in _DISPLAY_TO_SLUG:          # arguments were swapped
        s, r = _DISPLAY_TO_SLUG[s.lower()], s
    slug = s or _DISPLAY_TO_SLUG.get(r.lower(), r.lower().replace(" ", "_"))
    return slug, SOURCES.get(slug, r or slug)


# ---------------------------------------------------------------- sweep queue
# The extension asks for one product URL at a time; progress is persisted so a
# browser restart or a crashed page resumes instead of starting over.
_queue: list[dict] = []
_state: dict = {"done": {}, "attempts": {}, "inflight": {}, "partial": {}}


def load_queue() -> None:
    global _queue, _state
    _queue = json.loads(TARGETS.read_text(encoding="utf-8")) if TARGETS.exists() else []
    if STATE.exists():
        try:
            _state = json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            _state = {"done": {}, "attempts": {}, "inflight": {}, "partial": {}}
    _state.setdefault("done", {})
    _state.setdefault("attempts", {})
    _state.setdefault("inflight", {})
    _state.setdefault("partial", {})


def save_state() -> None:
    STATE.write_text(json.dumps(_state), encoding="utf-8")


# An attempt is counted on hand-out, so this is the number of tries, not retries.
# Two was too strict: the first targets of a run are handed out while the tab is
# still on the previous page and the extension is settling, which burned both
# tries on the highest-value products before the sweep was healthy.
MAX_ATTEMPTS = 5
INFLIGHT_TIMEOUT = 180        # seconds before a handed-out page is considered stuck


def next_target() -> dict | None:
    """Hand out the next unvisited URL.

    A URL just handed out is in flight and must not be handed out again until it
    either reports back or goes stale, otherwise the sweep visits the same product
    repeatedly. Pages that go stale twice are skipped so one broken product cannot
    stall the whole queue.
    """
    now = time.time()
    for t in _queue:
        u = t["url"]
        if u in _state["done"]:
            continue
        started = _state["inflight"].get(u)
        if started and (now - started) < INFLIGHT_TIMEOUT:
            continue                                   # someone is on it right now
        if _state["attempts"].get(u, 0) >= MAX_ATTEMPTS:
            continue
        _state["attempts"][u] = _state["attempts"].get(u, 0) + 1
        _state["inflight"][u] = now
        save_state()
        return t
    return None


def note_partial(url: str, n_reviews: int) -> None:
    """Count reviews arriving in mid-product chunks, and keep the page in flight
    while it is still actively delivering."""
    if not url or not n_reviews:
        return
    key = str(url).split("?")[0]
    _state["partial"][key] = _state["partial"].get(key, 0) + n_reviews
    for t in _queue:
        if t["url"].split("?")[0] == key and t["url"] in _state["inflight"]:
            _state["inflight"][t["url"]] = time.time()   # still alive
    save_state()


def mark_done(url: str, n_reviews: int) -> None:
    if not url:
        return
    want = str(url).split("?")[0]
    for t in _queue:
        if t["url"].split("?")[0] == want:
            total = _state["partial"].pop(want, 0) + n_reviews
            _state["done"][t["url"]] = total
            _state["inflight"].pop(t["url"], None)
            save_state()
            return


# ---------------------------------------------------- listing crawl queue
# Same hand-out pattern as the review sweep, over seeded category pages.
_listing: list[dict] = []
_listing_state: dict = {"done": {}, "inflight": {}}


def load_listing_queue() -> None:
    global _listing, _listing_state
    _listing = json.loads(LIST_TARGETS.read_text(encoding="utf-8")) if LIST_TARGETS.exists() else []
    if LIST_STATE.exists():
        try:
            _listing_state = json.loads(LIST_STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            _listing_state = {"done": {}, "inflight": {}}
    _listing_state.setdefault("done", {})
    _listing_state.setdefault("inflight", {})


def save_listing_state() -> None:
    LIST_STATE.write_text(json.dumps(_listing_state), encoding="utf-8")


LISTING_TIMEOUT = 900       # a 60-click page legitimately takes several minutes


def next_listing() -> dict | None:
    now = time.time()
    for t in _listing:
        u = t["url"]
        if u in _listing_state["done"]:
            continue
        started = _listing_state["inflight"].get(u)
        if started and (now - started) < LISTING_TIMEOUT:
            continue
        _listing_state["inflight"][u] = now
        save_listing_state()
        return t
    return None


def mark_listing_done(url: str, n_products: int) -> None:
    """Match the full URL including ?page=N.

    Stripping the query here collapsed every paginated target onto page 1
    (".../wine?page=35" marked ".../wine" done), so pages were never recorded,
    were re-handed after the in-flight timeout, and the crawl could not finish.
    """
    if not url:
        return
    want = str(url)
    for t in _listing:
        if t["url"] == want:
            _listing_state["done"][t["url"]] = n_products
            _listing_state["inflight"].pop(t["url"], None)
            save_listing_state()
            return
    # Fall back to path-only for targets that carry no page param.
    base = want.split("?")[0]
    for t in _listing:
        if "?" not in t["url"] and t["url"] == base:
            _listing_state["done"][t["url"]] = n_products
            _listing_state["inflight"].pop(t["url"], None)
            save_listing_state()
            return


def listing_progress() -> dict:
    done = len(_listing_state["done"])
    return {"total": len(_listing), "done": done,
            "remaining": max(0, len(_listing) - done),
            "products_seen": sum(_listing_state["done"].values())}


def queue_progress() -> dict:
    done = len(_state["done"])
    harvested = sum(_state["done"].values())
    return {"total": len(_queue), "done": done,
            "remaining": max(0, len(_queue) - done), "reviews_harvested": harvested}


def _in_stock(raw):
    if raw is None:
        return None
    s = str(raw).lower()
    if "outofstock" in s.replace(" ", "") or "sold out" in s or "unavailable" in s:
        return False
    if "instock" in s.replace(" ", "") or "in stock" in s or "available" in s:
        return True
    return None


def _iso_date(raw):
    if not raw:
        return None
    s = str(raw)
    m = re.search(r"\d{4}-\d{2}-\d{2}", s)
    if m:
        return m.group(0)
    # Dan Murphy's prints "19 June 2026"
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", s)
    if m:
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(m.group(0), fmt).date().isoformat()
            except ValueError:
                continue
    try:
        return datetime.fromtimestamp(int(s), tz=timezone.utc).date().isoformat()
    except (ValueError, OSError, OverflowError):
        return None


# Listing pages carry no product_type, so the category often cannot be read from
# the name alone ("Portraits Of Clare Clare Valley Riesling" has no category word).
# The page path is a strong hint: /beer/all, /list/wine, /spirits/premix-drinks.
_URL_CATEGORY = [
    ("rtd", ("premix", "rtd", "seltzer")),
    ("cider", ("cider",)),
    ("beer", ("beer",)),
    ("wine", ("wine", "champagne", "sparkling")),
    ("spirits", ("spirit", "whisky", "whiskey", "gin", "vodka", "rum", "tequila", "liqueur")),
]


def category_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = str(url).split("?")[0].lower()
    for cat, hints in _URL_CATEGORY:
        if any(h in path for h in hints):
            return cat
    return None


def _resolve_category(sub, name_raw, desc, url_hint):
    """Name-derived category wins; the page hint rescues the ones it cannot see."""
    guessed = N.guess_category(sub, name_raw, desc)
    if guessed and guessed != "other":
        return guessed
    return url_hint or guessed or "other"


def normalize_product(item: dict, url_hint: str | None = None) -> list[dict]:
    name_raw = item.get("name_raw")
    if not name_raw:
        return []
    slug, display = canon_source(item.get("source"), item.get("retailer"))
    brand = item.get("brand_raw")
    desc = item.get("description_raw") or ""
    sub = item.get("subcategory_raw") or ""
    blob = f"{name_raw} {sub} {desc}"

    volume_ml = N.parse_volume_ml(blob)
    # The retailer's own unit label beats guessing from the product name.
    pack_size = N.parse_pack_size(item.get("unit_raw") or "") or N.parse_pack_size(blob) or 1
    vintage = N.parse_vintage(name_raw)
    abv = N.parse_abv(blob)
    name = N.clean_name(name_raw)
    key = N.product_key(brand, name, volume_ml, vintage)
    price = N.parse_price(item.get("price_raw"))
    was = N.parse_price(item.get("was_price_raw"))
    ts = N.now_iso()

    out = []
    if key not in _seen_products:
        _seen_products.add(key)
        out.append({
            "record_type": "product",
            "product_key": key,
            "source": slug or "unknown",
            "source_url": item.get("source_url"),
            "source_product_id": item.get("source_product_id"),
            "name": name,
            "brand": brand,
            "category": _resolve_category(sub, name_raw, desc, url_hint),
            "subcategory": sub or None,
            "volume_ml": volume_ml,
            "pack_size": pack_size,
            "abv": abv,
            "vintage": vintage,
            "country": None,
            "region": None,
            "image_url": item.get("image_url"),
            "gtin": item.get("gtin"),
            "scraped_at": ts,
        })
        STATS["products"] += 1

    member_price = N.parse_price(item.get("member_price_raw"))
    # Re-scraping a page must not append a second identical offer. One row per
    # product/retailer/price/day; a genuine price change still creates a new row.
    offer_sig = (key, display, price, member_price, ts[:10])
    if offer_sig in _seen_offers:
        STATS["skipped"] += 1
    elif price is not None or member_price is not None:
        _seen_offers.add(offer_sig)
        out.append({
            "record_type": "offer",
            "product_key": key,
            "retailer": display,
            "url": item.get("source_url"),
            "price_aud": price,
            "was_price_aud": was if (was and was > price) else None,
            "unit_price_aud": round((price or member_price) / pack_size, 2) if pack_size > 1 else (price or member_price),
            "member_price_aud": member_price,
            "promo": item.get("promo_raw"),
            "in_stock": _in_stock(item.get("availability_raw")),
            "pack_size": pack_size,
            "postcode": item.get("postcode"),
            "aggregate_rating_norm": N.norm_rating(item.get("aggregate_rating_raw"),
                                                   item.get("aggregate_rating_scale")),
            "aggregate_review_count": item.get("aggregate_review_count"),
            "scraped_at": ts,
        })
        STATS["offers"] += 1
    return out


def normalize_review(item: dict) -> list[dict]:
    text = (item.get("text") or "").strip()
    if len(text) < 15:            # review text is the deliverable; drop score-only rows
        STATS["skipped"] += 1
        return []
    source = item.get("source") or "unknown"
    url = item.get("source_url") or ""
    author = item.get("author")
    rkey = N.review_key(source, url, author, text)
    if rkey in _seen_reviews:
        STATS["skipped"] += 1
        return []
    _seen_reviews.add(rkey)

    raw = item.get("rating_raw")
    scale = item.get("rating_scale") or (5 if raw is not None else None)
    STATS["reviews"] += 1
    return [{
        "record_type": "review",
        "review_key": rkey,
        "product_key": None,          # filled in by resolve.py
        "product_name_raw": item.get("product_name_raw"),
        "subject_type": item.get("subject_type") or "product",
        "source": source,
        "source_url": url,
        "author": author,
        "title": item.get("title"),
        "text": text,
        "rating_raw": raw,
        "rating_scale": scale,
        "rating_norm": N.norm_rating(raw, scale),
        # Which sort order produced this review. "Highest rating" is the site
        # default and heavily over-samples positive reviews; treat such rows as a
        # biased sample rather than a representative one.
        "sort_order": item.get("sort_order"),
        "review_date": _iso_date(item.get("review_date")),
        "scraped_at": N.now_iso(),
    }]


def normalize_summary(summary: dict) -> dict | None:
    """The retailer's own aggregate: authoritative, unlike an average over a sample."""
    if not summary:
        return None
    dist = {str(k): int(v) for k, v in (summary.get("distribution") or {}).items()}
    avg = summary.get("average_rating")
    scale = summary.get("rating_scale") or 5
    return {
        "record_type": "review_summary",
        "product_name_raw": summary.get("product_name_raw"),
        "source_url": summary.get("source_url"),
        "average_rating": avg,
        "rating_scale": scale,
        "average_rating_norm": N.norm_rating(avg, scale),
        "pct_recommend": summary.get("pct_recommend"),
        "distribution": dist,
        "total_reviews": summary.get("total_reviews"),
        "scraped_at": N.now_iso(),
    }


def append_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def handle_batch(batch: dict) -> dict:
    with _lock:
        STATS["batches"] += 1
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        # Write atomically: reprocess.py may read this directory while a sweep is
        # still running, and a half-written file is unreadable JSON.
        dest = RAW / f"batch_{stamp}.json"
        tmp = dest.with_suffix(".json.part")
        tmp.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")
        tmp.replace(dest)

        url_hint = category_from_url(batch.get("url"))
        prod_recs, rev_recs = [], []
        for item in batch.get("products", []):
            prod_recs.extend(normalize_product(item, url_hint))
        for item in batch.get("reviews", []):
            rev_recs.extend(normalize_review(item))

        if batch.get("listing_url"):
            mark_listing_done(batch["listing_url"], len(batch.get("products", [])))
        if batch.get("sweep_url"):
            mark_done(batch["sweep_url"], len(rev_recs))
        elif rev_recs:
            note_partial(batch.get("url"), len(rev_recs))

        if prod_recs:
            append_jsonl(OUT / "extension_products.jsonl", prod_recs)
        if rev_recs:
            append_jsonl(OUT / "extension_reviews.jsonl", rev_recs)
        summary = normalize_summary(batch.get("review_summary"))
        if summary:
            append_jsonl(OUT / "extension_review_summaries.jsonl", [summary])

        print(f"  batch from {batch.get('host')}: "
              f"+{sum(1 for r in prod_recs if r['record_type']=='product')} products, "
              f"+{sum(1 for r in prod_recs if r['record_type']=='offer')} offers, "
              f"+{len(rev_recs)} reviews  | totals {STATS}")
        return dict(STATS)


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        if self.path.startswith("/next_listing"):
            with _lock:
                t = next_listing()
                payload = {"target": t, "progress": listing_progress()}
            body = json.dumps(payload).encode()
        elif self.path.startswith("/next"):
            with _lock:
                t = next_target()
                payload = {"target": t, "progress": queue_progress()}
            body = json.dumps(payload).encode()
        elif self.path.startswith("/progress"):
            with _lock:
                body = json.dumps({"stats": STATS, "queue": queue_progress(),
                                   "listing": listing_progress()}).encode()
        else:
            body = json.dumps(STATS).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._cors(); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            batch = json.loads(self.rfile.read(n))
            stats = handle_batch(batch)
            body = json.dumps({"ok": True, "stats": stats}).encode()
            self.send_response(200)
        except Exception as e:                    # noqa: BLE001 - report to the extension
            body = json.dumps({"ok": False, "error": str(e)}).encode()
            self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self._cors(); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):                    # keep stdout to our own summaries
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    load_queue()
    load_listing_queue()
    print(f"Collector listening on http://127.0.0.1:{port}  -> {OUT}")
    if _listing:
        lp = listing_progress()
        print(f"Listing queue: {lp['total']} pages, {lp['done']} already crawled")
    if _queue:
        p = queue_progress()
        print(f"Review sweep queue: {p['total']} targets, {p['done']} already done")
    else:
        print("No review queue loaded (run build_queue.py to create one)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
