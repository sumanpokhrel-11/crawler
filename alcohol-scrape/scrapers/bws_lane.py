"""BWS — HTTP lane via the sitemap plus Endeavour's own product API.

BWS sits behind Cloudflare and its storefront is an Angular SPA: the product
page HTML is a 7KB shell with no prices, no names and no reviews. Two things
make an HTTP lane possible anyway.

1. robots.txt publishes a product sitemap, and every URL in it carries the
   stockcode:  /product/<stockcode>/<url-friendly-name>.  That is the whole
   catalogue enumeration, sanctioned by the site, with no category crawl.

2. The same Endeavour API that backs Dan Murphy's answers for BWS:

       GET https://api.bws.com.au/apis/ui/Products/<code,code,...>

   It takes stockcodes in batches and returns name, brand, price, was-price,
   package size, availability, the aggregate rating and the review count, plus
   an AdditionalDetails list carrying ABV, country/state/region, vintage,
   varietal and standard drinks. No store or session cookie is needed.

Cloudflare rate-limits hard: a 200-request burst at concurrency 8 earned an
immediate 429 "Just a moment" challenge that took a minute to clear. Requests
are therefore serial with a pause between them, and a 429 backs off and retries
rather than dropping the batch.

    python3 scrapers/bws_lane.py [--batch 30] [--limit N] [--delay 1.5]
"""
from __future__ import annotations

import argparse
import gzip
import html as html_mod
import json
import random
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import normalize as N

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "out"
SITEMAP = "https://bws.com.au/sitemap/products-sitemap.xml"
API = "https://api.bws.com.au/apis/ui/Products/"
SOURCE, RETAILER = "bws", "BWS"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
           "Origin": "https://bws.com.au", "Referer": "https://bws.com.au/",
           "Accept-Encoding": "gzip"}


def get(url: str, timeout: int = 45, tries: int = 5):
    """GET with backoff. Cloudflare answers 429 with an HTML challenge page."""
    delay = 5.0
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return data
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < tries - 1:
                print(f"    throttled ({e.code}), waiting {delay:.0f}s")
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == tries - 1:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def stockcodes() -> list[tuple[str, str]]:
    """(stockcode, product url) for every entry in the sitemap."""
    xml = get(SITEMAP, timeout=90).decode("utf-8", "ignore")
    seen, out = set(), []
    for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml):
        m = re.search(r"/product/(\d+)/", loc)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append((m.group(1), loc))
    return out


def detail(product: dict) -> dict:
    """AdditionalDetails is a list of {Name, Value}; index it by name."""
    return {a.get("Name"): a.get("Value")
            for a in (product.get("AdditionalDetails") or []) if a.get("Name")}


def txt(value) -> str | None:
    """Some AdditionalDetails values are lists (categorynodename, for one)."""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value if v is not None)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


# AdditionalDetails stores images as a bare filename ("122063-1.png"); the
# storefront serves them from Endeavour's Scene7 CDN, extension stripped.
def image_url(value) -> str | None:
    name = txt(value)
    if not name:
        return None
    if name.startswith("http"):
        return name
    return "https://egl-assets.scene7.com/is/image/endeavour/" + re.sub(
        r"\.(png|jpe?g|webp)$", "", name, flags=re.I)


def strip_html(value) -> str | None:
    raw = txt(value)
    if not raw:
        return None
    text = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html_mod.unescape(text)).strip() or None


def as_float(value):
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def to_records(p: dict, url: str) -> list[dict]:
    name_raw = (p.get("Name") or "").strip()
    if not name_raw:
        return []
    d = detail(p)
    # productname carries the vintage that Name usually drops
    # ("... 750ML 2021" vs "Xanadu Margaret River Cabernet Sauvignon").
    full = txt(d.get("productname")) or name_raw
    brand = p.get("BrandName") or txt(d.get("brand_name"))
    volume_ml = N.parse_volume_ml(p.get("PackageSize")) or N.parse_volume_ml(full)
    pack = N.parse_pack_size(full) or N.parse_pack_size(name_raw) or 1
    vintage = N.parse_vintage(txt(d.get("vintage")) or "") or N.parse_vintage(full)
    name = N.clean_name(name_raw)
    key = N.product_key(brand, name, volume_ml, vintage)
    ts = N.now_iso()

    recs = [{
        "record_type": "product", "product_key": key, "source": SOURCE,
        "source_url": url, "source_product_id": str(p.get("Stockcode") or ""),
        "name": name, "brand": brand,
        "category": N.guess_category(txt(d.get("standardcategory")),
                                     txt(d.get("categorynodename")), name_raw),
        "subcategory": txt(d.get("liquorstyle")) or txt(d.get("varietal"))
                       or txt(d.get("categorynodename")),
        "volume_ml": volume_ml, "pack_size": pack,
        "abv": as_float(d.get("alcohol%")) or N.parse_abv(full),
        "vintage": vintage,
        "country": txt(d.get("countryoforigin")),
        "region": txt(d.get("regionoforigin")) or txt(d.get("stateoforigin")),
        "image_url": image_url(d.get("image1")),
        "gtin": None, "scraped_at": ts,
        "standard_drinks": as_float(d.get("standarddrinks")),
        "description": strip_html(d.get("productcopy")),
    }]

    price = N.parse_price(p.get("Price"))
    if price is not None:
        was = N.parse_price(p.get("WasPrice"))
        recs.append({
            "record_type": "offer", "product_key": key, "retailer": RETAILER,
            "url": url, "price_aud": price,
            "was_price_aud": was if was and was > price else None,
            "member_price_aud": None,
            "promo": "special" if p.get("IsOnSpecial") else None,
            "unit_price_aud": round(price / pack, 2) if pack > 1 else price,
            "in_stock": p.get("IsAvailable"),
            "pack_size": pack, "postcode": None, "store": None,
            "aggregate_rating_norm": N.norm_rating(p.get("OverallRating"), 5),
            "aggregate_review_count": p.get("NumberOfReviews") or None,
            "scraped_at": ts,
        })
    return recs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=30,
                    help="stockcodes per API call (30 verified working)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N products")
    ap.add_argument("--delay", type=float, default=1.5)
    args = ap.parse_args()

    codes = stockcodes()
    if args.limit:
        codes = codes[:args.limit]
    print(f"sitemap: {len(codes)} products")

    urls = dict(codes)
    out_path = OUT / "bws.jsonl"
    written = {"products": 0, "offers": 0}
    missing = 0

    with out_path.open("w", encoding="utf-8") as fh:
        for i in range(0, len(codes), args.batch):
            chunk = [c for c, _ in codes[i:i + args.batch]]
            try:
                raw = get(API + ",".join(chunk))
                data = json.loads(raw.decode("utf-8", "ignore"))
            except (urllib.error.HTTPError, urllib.error.URLError,
                    TimeoutError, OSError, ValueError) as e:
                print(f"  ! batch {i // args.batch + 1} failed: {e}")
                continue
            items = data if isinstance(data, list) else list(data.values())
            got = set()
            for p in items:
                if not isinstance(p, dict) or not p.get("Stockcode"):
                    continue
                code = str(p["Stockcode"])
                got.add(code)
                for r in to_records(p, urls.get(code, f"https://bws.com.au/product/{code}/")):
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                    written["products" if r["record_type"] == "product" else "offers"] += 1
            missing += len(set(chunk) - got)
            done = min(i + args.batch, len(codes))
            print(f"  {done}/{len(codes)}: {written['products']} products, "
                  f"{written['offers']} offers, {missing} not returned")
            time.sleep(args.delay + random.uniform(0, 0.5))

    print(f"done: {written} -> {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
