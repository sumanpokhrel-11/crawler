"""Rebuild data/out from the archived raw batches in data/raw/extension.

Every batch the extension ever sent is kept on disk, so a parser or schema fix can
be applied to already-collected data without re-crawling the sites. Run this after
changing normalize.py or collector.py.

    python3 scrapers/reprocess.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collector as C

RAW = C.ROOT / "data" / "raw" / "extension"
OUT = C.OUT


def main() -> None:
    batches = sorted(RAW.glob("*.json"))
    if not batches:
        print(f"no raw batches in {RAW}")
        return

    for f in (OUT / "extension_products.jsonl", OUT / "extension_reviews.jsonl",
              OUT / "extension_review_summaries.jsonl"):
        if f.exists():
            f.unlink()
    C._seen_products.clear()
    C._seen_reviews.clear()
    C._seen_offers.clear()

    prod_recs, rev_recs, sum_recs = [], [], []
    for f in batches:
        try:
            batch = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"  ! skipping unreadable {f.name}")
            continue
        hint = C.category_from_url(batch.get("url"))
        for item in batch.get("products", []):
            prod_recs.extend(C.normalize_product(item, hint))
        for item in batch.get("reviews", []):
            rev_recs.extend(C.normalize_review(item))
        summary = C.normalize_summary(batch.get("review_summary"))
        if summary:
            sum_recs.append(summary)

    if prod_recs:
        C.append_jsonl(OUT / "extension_products.jsonl", prod_recs)
    if rev_recs:
        C.append_jsonl(OUT / "extension_reviews.jsonl", rev_recs)
    if sum_recs:
        C.append_jsonl(OUT / "extension_review_summaries.jsonl", sum_recs)

    products = sum(1 for r in prod_recs if r["record_type"] == "product")
    offers = sum(1 for r in prod_recs if r["record_type"] == "offer")
    print(f"replayed {len(batches)} batches -> {products} products, "
          f"{offers} offers, {len(rev_recs)} reviews")


if __name__ == "__main__":
    main()
