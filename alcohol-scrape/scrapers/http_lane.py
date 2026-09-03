"""HTTP lane: for sources that answer a plain request.

Currently drives any Shopify storefront via /products.json, which hands over the
whole catalogue (products + variants + prices) with no HTML parsing at all.
Confirmed working against Kent Street Cellars.
"""
from __future__ import annotations

import gzip
import json
import random
import time
import urllib.error
import urllib.request
from pathlib import Path

import normalize as N

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "out"


def fetch(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip",
        "Accept-Language": "en-AU,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


def polite_sleep(base: float = 1.2) -> None:
    """Keep well under any sane rate limit and avoid a metronomic request pattern."""
    time.sleep(base + random.uniform(0, 0.6))


def scrape_shopify(source: str, retailer: str, base_url: str, max_pages: int = 200) -> list[dict]:
    """Walk /products.json. Emits one product record plus one offer record per variant."""
    records: list[dict] = []
    seen_keys: set[str] = set()
    base_url = base_url.rstrip("/")

    for page in range(1, max_pages + 1):
        url = f"{base_url}/products.json?limit=250&page={page}"
        try:
            payload = json.loads(fetch(url))
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            print(f"  ! {source} page {page} failed: {e}")
            break

        products = payload.get("products") or []
        if not products:
            break

        # Archive the raw response so a parser change never needs a re-crawl.
        (RAW / source).mkdir(parents=True, exist_ok=True)
        (RAW / source / f"products_p{page}.json").write_bytes(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        for p in products:
            records.extend(_shopify_product(source, retailer, base_url, p, seen_keys))

        print(f"  {source}: page {page} -> {len(products)} products (total records {len(records)})")
        if len(products) < 250:
            break
        polite_sleep()

    return records


def _shopify_product(source, retailer, base_url, p, seen_keys) -> list[dict]:
    out = []
    handle = p.get("handle", "")
    url = f"{base_url}/products/{handle}"
    title = p.get("title") or ""
    vendor = p.get("vendor") or None
    ptype = p.get("product_type") or ""
    tags = " ".join(p.get("tags") or []) if isinstance(p.get("tags"), list) else str(p.get("tags") or "")
    body = p.get("body_html") or ""
    images = p.get("images") or []
    image_url = images[0].get("src") if images else None

    for v in (p.get("variants") or []):
        vtitle = v.get("title") or ""
        # Volume/pack can live in the product title or the variant title ("750ml" vs "Case of 6").
        blob = f"{title} {vtitle} {ptype} {tags}"
        volume_ml = N.parse_volume_ml(blob)
        pack_size = N.parse_pack_size(blob) or 1
        vintage = N.parse_vintage(title)
        abv = N.parse_abv(f"{title} {body}")
        category = N.guess_category(ptype, tags, title)

        name = N.clean_name(title)
        if vtitle and vtitle.lower() not in ("default title", "default"):
            name_full = f"{name} {N.clean_name(vtitle)}".strip()
        else:
            name_full = name

        key = N.product_key(vendor, name_full, volume_ml, vintage)
        price = N.parse_price(v.get("price"))
        was = N.parse_price(v.get("compare_at_price"))

        if key not in seen_keys:
            seen_keys.add(key)
            out.append({
                "record_type": "product",
                "product_key": key,
                "source": source,
                "source_url": url,
                "source_product_id": str(p.get("id")) if p.get("id") else None,
                "name": name_full,
                "brand": vendor,
                "category": category,
                "subcategory": ptype or None,
                "volume_ml": volume_ml,
                "pack_size": pack_size,
                "abv": abv,
                "vintage": vintage,
                "country": None,
                "region": None,
                "image_url": image_url,
                "gtin": v.get("barcode") or None,
                "scraped_at": N.now_iso(),
            })

        out.append({
            "record_type": "offer",
            "product_key": key,
            "retailer": retailer,
            "url": url,
            "price_aud": price,
            "was_price_aud": was if (was and price and was > price) else None,
            "unit_price_aud": round(price / pack_size, 2) if (price and pack_size > 1) else price,
            "in_stock": bool(v.get("available")),
            "pack_size": pack_size,
            "postcode": None,
            "scraped_at": N.now_iso(),
        })
    return out


def write_jsonl(records: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# Sources confirmed reachable over plain HTTP during the Phase 1 audit.
SHOPIFY_SOURCES = [
    ("kent_street_cellars", "Kent Street Cellars", "https://kentstreetcellars.com.au"),
]

if __name__ == "__main__":
    import sys
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    for source, retailer, base in SHOPIFY_SOURCES:
        print(f"[{source}]")
        recs = scrape_shopify(source, retailer, base, max_pages=limit)
        write_jsonl(recs, OUT / f"{source}.jsonl")
        prods = sum(1 for r in recs if r["record_type"] == "product")
        offers = sum(1 for r in recs if r["record_type"] == "offer")
        print(f"  -> {prods} products, {offers} offers -> data/out/{source}.jsonl")
