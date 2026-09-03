"""Build the product-page review sweep queue from the catalogue.

Listing crawls give every product's URL and its advertised review count. This ranks
them so the sweep spends its time where the review text actually is, and writes a
queue the collector hands out one URL at a time.

    python3 scrapers/build_queue.py [--min-reviews N] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "out"
CATALOGUE = OUT / "catalogue.json"
TARGETS = OUT / "review_targets.json"
STATE = OUT / "review_sweep_state.json"

# Retailers with a product-page review adapter.
SUPPORTED = {"Dan Murphy's", "BWS", "Liquorland"}

# Dan Murphy's listing pages advertise a review count, so its queue can be ranked
# and filtered by volume. Liquorland exposes no count until the product page is
# rendered, so every product is a candidate and --min-reviews cannot apply.
NO_COUNT_RETAILERS = {"Liquorland"}

# How many reviews to take per product. Depth past this adds little signal and
# costs a lot of time: reviews come 4 per page, and Baileys alone advertises 6,936.
# Reviews are collected newest-first, so a cap keeps a recent, representative slice.
def review_cap(expected: int) -> int | None:
    """None means take everything the product has."""
    if expected >= 200:
        return 50
    if expected > 20:
        return 20
    return None          # 20 or fewer: take the lot


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-reviews", type=int, default=1,
                    help="skip products with fewer advertised reviews than this")
    ap.add_argument("--limit", type=int, default=0, help="cap the queue length (0 = no cap)")
    ap.add_argument("--retailer", default=None,
                    help="build the queue for one retailer only, e.g. Liquorland")
    args = ap.parse_args()

    if not CATALOGUE.exists():
        print(f"no catalogue at {CATALOGUE} — run resolve.py first")
        return
    catalogue = json.loads(CATALOGUE.read_text(encoding="utf-8"))

    targets: dict[str, dict] = {}
    for p in catalogue:
        for o in p.get("offers", []):
            # Strip tracking params (?isFromSearch=...) so the same product page
            # cannot enter the queue twice under two URLs.
            url = (o.get("url") or "").split("?")[0]
            retailer = o.get("retailer")
            if retailer not in SUPPORTED:
                continue
            if args.retailer and retailer != args.retailer:
                continue
            # Dan Murphy's product URLs contain /product/; Liquorland uses
            # /<category>/<slug>_<id>, so accept either shape.
            if "/product/" not in url and not re.search(r"_\d+$", url):
                continue
            count = o.get("aggregate_review_count") or 0
            if o.get("retailer") not in NO_COUNT_RETAILERS and count < args.min_reviews:
                continue
            # One entry per URL, keeping the highest advertised count.
            prev = targets.get(url)
            if not prev or count > prev["expected_reviews"]:
                targets[url] = {
                    "url": url,
                    "product_key": p["product_key"],
                    "name": p["name"],
                    "retailer": o["retailer"],
                    "expected_reviews": count,
                    "max_reviews": review_cap(count),
                }

    queue = sorted(targets.values(), key=lambda t: -t["expected_reviews"])
    if args.limit:
        queue = queue[: args.limit]

    TARGETS.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    if STATE.exists():
        STATE.unlink()      # a new queue invalidates old progress

    total = sum(t["expected_reviews"] for t in queue)
    planned = sum(min(t["expected_reviews"], t["max_reviews"] or t["expected_reviews"])
                  for t in queue)
    pages = sum(-(-min(t["expected_reviews"], t["max_reviews"] or t["expected_reviews"]) // 4)
                for t in queue)
    print(f"queued {len(queue)} product pages, {total} reviews advertised")
    print(f"will collect ~{planned} reviews over ~{pages} page turns "
          f"(~{(pages * 2.0 + len(queue) * 11.0) / 3600:.1f} h)")
    print(f"-> {TARGETS.relative_to(ROOT)}")
    for t in queue[:5]:
        print(f"  {t['expected_reviews']:6}  {t['name'][:50]}")


if __name__ == "__main__":
    main()
