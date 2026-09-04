"""Thirsty Camel — HTTP lane via their own backend API.

The storefront is a Next.js SPA with no prices in the HTML, but its backend is a
public Cloud Run service:

    GET {CORE}/products/search?limit=100&page=N&totals=true&store=<storeId>
    headers: Accept: application/json, Bypass-Tunnel-Reminder: true

Thirsty Camel is a franchise, so pricing is per store — the `store` parameter is
what makes prices appear at all (without it every `pricing` field is null).
Prices are integer cents. Store choice is recorded on every offer.

    python3 scrapers/thirstycamel_lane.py [--limit-pages N]
"""
from __future__ import annotations

import argparse
import gzip
import json
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import normalize as N

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "out"
CORE = "https://production-core-onnsxgivka-ts.a.run.app"
SOURCE, RETAILER = "thirsty_camel", "Thirsty Camel"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
           "Bypass-Tunnel-Reminder": "true", "Accept-Encoding": "gzip"}


def get(url: str, timeout: int = 40):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return json.loads(data.decode("utf-8", "ignore"))


def pick_store() -> tuple[str, str]:
    """Pick the store with the widest range.

    Thirsty Camel is a franchise: the catalogue and the prices are per store, and
    a small country outlet lists a fraction of what a larger one does. Sampling
    keeps the run to a single store so prices stay internally consistent.
    (`/stores?limit=60` returns HTTP 400 — 20 is accepted.)
    """
    stores = []
    for page in (1, 2, 3):
        try:
            stores += get(f"{CORE}/stores?limit=20&page={page}").get("items", [])
        except (urllib.error.URLError, OSError):
            break
    best = (0, None, None)
    for st in stores:
        sid = st.get("id")
        if not sid:
            continue
        try:
            meta = get(f"{CORE}/products/search?limit=1&page=1&totals=true"
                       f"&store={sid}").get("results", {}).get("meta", {})
        except (urllib.error.URLError, OSError):
            continue
        n = meta.get("totalItems", 0)
        if n > best[0]:
            best = (n, sid, st.get("slug") or sid)
    if not best[1]:
        raise SystemExit("no usable store found")
    print(f"widest range: {best[2]} ({best[0]} items)")
    return best[1], best[2]


def price_of(item: dict) -> tuple[float | None, bool]:
    """pricing.price is integer cents; specialPricing overrides when active."""
    best, special = None, False
    for node, is_special in ((item.get("pricing"), False), (item.get("specialPricing"), True)):
        if isinstance(node, list):
            node = node[0] if node else None
        if not isinstance(node, dict):
            continue
        cents = node.get("price")
        if isinstance(cents, (int, float)) and cents > 0:
            value = round(cents / 100.0, 2)
            if best is None or (is_special and value < best):
                best, special = value, is_special or special
    return best, special


def to_records(item: dict, store_slug: str) -> list[dict]:
    name_raw = item.get("liquorfileName") or item.get("name") or ""
    if not name_raw:
        return []
    # packQty is the CARTON quantity (units per shipping case), not what the
    # customer buys: "19 Crimes Hard Chardonnay 750ml" comes back with packQty 6
    # while $16.00 is the price of one bottle. Using it made unit prices absurd
    # ($1.25 for St Hallett Shiraz) and blocked like-for-like comparison.
    # The sellable pack, when there is one, is stated in the name ("... 6pk").
    pack = N.parse_pack_size(name_raw) or 1
    unit = item.get("unitSize")
    volume_ml = None
    try:
        volume_ml = int(float(unit)) if unit else None
    except (TypeError, ValueError):
        volume_ml = N.parse_volume_ml(name_raw)
    if not volume_ml:
        volume_ml = N.parse_volume_ml(name_raw)

    vintage = N.parse_vintage(name_raw)
    name = N.clean_name(name_raw)
    key = N.product_key(None, name, volume_ml, vintage)
    barcodes = item.get("barcodes") or []
    gtin = next((b for b in barcodes if str(b).isdigit() and len(str(b)) in (8, 12, 13, 14)), None)
    group = item.get("group") or {}
    group = group.get("name") if isinstance(group, dict) else group
    ts = N.now_iso()

    recs = [{
        "record_type": "product", "product_key": key, "source": SOURCE,
        "source_url": f"https://www.thirstycamel.com.au/product/{item.get('slug','')}",
        "source_product_id": str(item.get("liquorfileNo") or item.get("id") or ""),
        "name": name, "brand": None,
        "category": N.guess_category(group, name_raw),
        "subcategory": group, "volume_ml": volume_ml, "pack_size": pack,
        "abv": N.parse_abv(name_raw), "vintage": vintage,
        "country": None, "region": None, "image_url": None,
        "gtin": gtin, "scraped_at": ts,
        "carton_qty": item.get("packQty"),
    }]
    price, special = price_of(item)
    if price is not None:
        recs.append({
            "record_type": "offer", "product_key": key, "retailer": RETAILER,
            "url": recs[0]["source_url"], "price_aud": price,
            "was_price_aud": None, "member_price_aud": None,
            "promo": "special" if special else None,
            "unit_price_aud": round(price / pack, 2) if pack > 1 else price,
            "in_stock": None, "pack_size": pack,
            "postcode": None, "store": store_slug,
            "aggregate_rating_norm": None, "aggregate_review_count": None,
            "scraped_at": ts,
        })
    return recs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-pages", type=int, default=0)
    args = ap.parse_args()

    store_id, store_slug = pick_store()
    print(f"store: {store_slug}")
    out_path = OUT / "thirstycamel.jsonl"
    written = {"products": 0, "offers": 0}

    with out_path.open("w", encoding="utf-8") as fh:
        page, pages = 1, 1
        while page <= pages:
            url = (f"{CORE}/products/search?limit=100&page={page}"
                   f"&totals=true&store={store_id}")
            try:
                j = get(url)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                print(f"  ! page {page} failed: {e}")
                break
            res = j.get("results", {})
            meta = res.get("meta", {})
            # totalPages has been seen to understate the catalogue, so keep going
            # while pages still return items.
            reported = meta.get("totalPages", 1)
            items = res.get("items", [])
            pages = max(reported, page + 1) if items else page
            if args.limit_pages:
                pages = min(pages, args.limit_pages)
            for item in items:
                for r in to_records(item, store_slug):
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                    written["products" if r["record_type"] == "product" else "offers"] += 1
            print(f"  page {page}/{pages}: {written['products']} products, "
                  f"{written['offers']} offers")
            page += 1
            time.sleep(0.6 + random.uniform(0, 0.5))

    print(f"done: {written} -> {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
