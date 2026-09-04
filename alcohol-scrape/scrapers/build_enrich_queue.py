"""Build the Dan Murphy's price-enrichment queue.

Listing pages only render a price for tiles near the viewport, so a deep crawl
returns most products with a name and no price (78% of tiles, measured). Dan
Murphy's own API returns price, pack size, rating and review count for a list of
stockcodes in one call, and every product URL contains its stockcode — so the
gap can be filled without loading a single page.

    python3 scrapers/build_enrich_queue.py [--only-missing]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "out"
PRODUCTS = OUT / "extension_products.jsonl"
TARGETS = OUT / "enrich_targets.json"
STATE = OUT / "enrich_state.json"

STOCKCODE = re.compile(r"/product/DM_(\d+)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only-missing", action="store_true",
                    help="only products that currently have no offer")
    args = ap.parse_args()

    priced: set[str] = set()
    products: dict[str, str] = {}          # stockcode -> product_key
    for line in PRODUCTS.open(encoding="utf-8"):
        r = json.loads(line)
        if r["record_type"] == "offer" and r.get("retailer") == "Dan Murphy's":
            priced.add(r["product_key"])
        elif r["record_type"] == "product" and r.get("source") == "dan_murphys":
            m = STOCKCODE.search(r.get("source_url") or "")
            if m:
                products.setdefault(m.group(1), r["product_key"])

    codes = [c for c, key in products.items()
             if not args.only_missing or key not in priced]

    TARGETS.write_text(json.dumps({"stockcodes": codes,
                                   "index": products}, ensure_ascii=False), encoding="utf-8")
    if STATE.exists():
        STATE.unlink()
    print(f"queued {len(codes)} stockcodes for API enrichment "
          f"({len(products)} known, {len(priced)} already priced)")
    print(f"-> {TARGETS.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
