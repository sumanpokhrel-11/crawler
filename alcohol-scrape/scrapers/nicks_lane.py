"""Nicks Wine Merchants — HTTP lane.

Nuxt storefront that server-renders a Product JSON-LD block on every product
page, and publishes a product sitemap. No browser, no extension: read the
sitemap, fetch each product, parse the structured data.

    python3 scrapers/nicks_lane.py [--limit N] [--workers 6]
"""
from __future__ import annotations

import argparse
import gzip
import html as html_mod
import json
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import normalize as N

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "out"
RAW = ROOT / "data" / "raw" / "nicks"
SITEMAP = "https://www.nicks.com.au/public/sitemap-products.xml"
SOURCE, RETAILER = "nicks", "Nicks Wine Merchants"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
LD = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)

_lock = threading.Lock()


def fetch(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml",
        "Accept-Encoding": "gzip", "Accept-Language": "en-AU,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data.decode("utf-8", "ignore")


def product_urls() -> list[str]:
    return re.findall(r"<loc>([^<]+)</loc>", fetch(SITEMAP, timeout=60))


def _valid_gtin(value) -> str | None:
    """A GTIN is 8, 12, 13 or 14 digits. Anything else is an internal id."""
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    return digits if len(digits) in (8, 12, 13, 14) else None


def parse_product(html: str, url: str) -> list[dict] | None:
    """Pull the Product node out of the page's structured data."""
    node = None
    for block in LD.findall(html):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        for n in (data if isinstance(data, list) else [data]):
            if isinstance(n, dict) and n.get("@type") == "Product":
                node = n
                break
        if node:
            break
    if not node:
        return None

    offers = node.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    # JSON-LD carries HTML entities ("O&#x27;Leary"), which would otherwise end
    # up in the product name and break matching against other retailers.
    name_raw = html_mod.unescape(node.get("name") or "")
    brand = node.get("brand")
    brand = brand.get("name") if isinstance(brand, dict) else brand
    if isinstance(brand, str):
        brand = html_mod.unescape(brand)

    # "Generous Gin (700ml)" — the size lives in the trailing bracket.
    blob = f"{name_raw} {node.get('description') or ''}"
    volume_ml = N.parse_volume_ml(blob)
    pack = N.parse_pack_size(blob) or 1
    vintage = N.parse_vintage(name_raw)
    name = N.clean_name(name_raw)
    key = N.product_key(brand, name, volume_ml, vintage)
    price = N.parse_price(offers.get("price"))
    avail = str(offers.get("availability") or "")
    ts = N.now_iso()

    recs = [{
        "record_type": "product", "product_key": key, "source": SOURCE,
        "source_url": url, "source_product_id": node.get("sku"),
        "name": name, "brand": brand,
        "category": N.guess_category(name_raw, node.get("description")),
        "subcategory": None, "volume_ml": volume_ml, "pack_size": pack,
        "abv": N.parse_abv(blob), "vintage": vintage,
        "country": None, "region": None,
        "image_url": node.get("image") if isinstance(node.get("image"), str) else None,
        # Nicks puts an internal hash in gtin13 ("E06C4B2E"), not a barcode.
        # Accepting it would create false cross-retailer matches, since GTIN is
        # the highest-confidence key in resolve.py.
        "gtin": _valid_gtin(node.get("gtin13") or node.get("gtin")),
        "scraped_at": ts,
    }]
    if price is not None:
        recs.append({
            "record_type": "offer", "product_key": key, "retailer": RETAILER,
            "url": url, "price_aud": price, "was_price_aud": None,
            "member_price_aud": None, "promo": None,
            "unit_price_aud": round(price / pack, 2) if pack > 1 else price,
            "in_stock": None if not avail else ("outofstock" not in avail.lower().replace("/", "")),
            "pack_size": pack, "postcode": None,
            "aggregate_rating_norm": None, "aggregate_review_count": None,
            "scraped_at": ts,
        })
    return recs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6,
                    help="concurrent requests; keep modest to stay polite")
    args = ap.parse_args()

    RAW.mkdir(parents=True, exist_ok=True)
    urls = product_urls()
    if args.limit:
        urls = urls[: args.limit]
    print(f"{len(urls)} product URLs from the sitemap")

    out_path = OUT / "nicks.jsonl"
    stats = {"ok": 0, "no_ld": 0, "err": 0, "offers": 0}
    fh = out_path.open("w", encoding="utf-8")

    def work(url: str) -> None:
        try:
            html = fetch(url)
        except (urllib.error.URLError, TimeoutError, OSError):
            with _lock:
                stats["err"] += 1
            return
        recs = parse_product(html, url)
        with _lock:
            if not recs:
                stats["no_ld"] += 1
            else:
                stats["ok"] += 1
                stats["offers"] += sum(1 for r in recs if r["record_type"] == "offer")
                for r in recs:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                if stats["ok"] % 200 == 0:
                    print(f"  {stats['ok']}/{len(urls)} ok, {stats['offers']} offers, "
                          f"{stats['err']} errors")
                    fh.flush()
        time.sleep(0.25 + random.uniform(0, 0.35))     # politeness, per worker

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(work, urls))
    fh.close()
    print(f"done: {stats} -> {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
