"""Entity resolution + export.

Two jobs:
  1. Cross-retailer product matching (so one bottle = one row with N offers).
  2. Attaching reviews to the product they describe.

Match strategy, highest confidence first:
  GTIN/barcode  ->  exact match slug  ->  fuzzy slug similarity (stdlib difflib).
Anything below the fuzzy threshold is written to review_queue.jsonl for a human,
rather than being guessed at and silently corrupting the comparison data.

    python3 scrapers/resolve.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import normalize as N

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "out"
FUZZY_THRESHOLD = 0.86     # below this a match goes to the human queue


def load_records() -> list[dict]:
    records = []
    for f in sorted(OUT.glob("*.jsonl")):
        if f.name in ("catalogue.json", "review_queue.jsonl", "products.jsonl"):
            continue
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return records


def _tokens(s: str) -> set[str]:
    return {t for t in s.split() if len(t) > 2}


def build_index(canonical: dict) -> tuple[dict, dict, dict]:
    """Inverted token index over canonical product names.

    Scoring every review against every product is O(reviews x products) and takes
    many minutes at this scale. Candidates that share no meaningful token can
    never clear the threshold, so an inverted index cuts the comparison set to a
    handful per review. Very common tokens (present in >5% of products) carry no
    signal and are skipped when gathering candidates.
    """
    slugs, tokens, postings = {}, {}, {}
    for key, p in canonical.items():
        slug = N.match_slug(p.get("brand"), p["name"], p.get("volume_ml"), p.get("vintage"))
        slugs[key] = slug
        toks = _tokens(slug)
        tokens[key] = toks
        for t in toks:
            postings.setdefault(t, []).append(key)
    cutoff = max(20, len(canonical) // 20)
    postings = {t: ks for t, ks in postings.items() if len(ks) <= cutoff}
    return slugs, tokens, postings


def review_slug(name_raw: str) -> str:
    """Slug for a review's product name.

    Product slugs carry volume and vintage, so a review slug built without them
    scores short of the threshold on otherwise identical names ("Heineken Cans
    500mL" vs "heineken cans 500ml" was 0.81 against a 0.86 cutoff). The review
    heading contains both, so parse them out and build the slug the same way.
    """
    return N.match_slug(None, name_raw,
                        N.parse_volume_ml(name_raw), N.parse_vintage(name_raw))


def best_match(name_raw: str, slugs: dict, tokens: dict, postings: dict):
    """Returns (product_key, score) using the inverted index for candidates."""
    target = review_slug(name_raw)
    if not target:
        return None, 0.0
    ttok = _tokens(target)
    if not ttok:
        return None, 0.0

    counts: dict[str, int] = {}
    for t in ttok:
        for key in postings.get(t, ()):  # rare tokens only
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None, 0.0

    # Score only the best-overlapping candidates.
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:40]
    best_key, best_score = None, 0.0
    for key, _ in ranked:
        score = SequenceMatcher(None, target, slugs[key]).ratio()
        if score > best_score:
            best_key, best_score = key, score
    return best_key, round(best_score, 4)


def main() -> None:
    records = load_records()
    summaries = [r for r in records if r["record_type"] == "review_summary"]
    products = [r for r in records if r["record_type"] == "product"]
    offers = [r for r in records if r["record_type"] == "offer"]
    reviews = [r for r in records if r["record_type"] == "review"]
    by_url = {(s.get("source_url") or "").split("?")[0]: s for s in summaries}
    print(f"loaded {len(products)} products, {len(offers)} offers, {len(reviews)} reviews")

    # --- 1. cross-retailer product merge --------------------------------------
    by_gtin: dict[str, str] = {}
    canonical: dict[str, dict] = {}
    alias: dict[str, str] = {}          # product_key -> canonical product_key

    for p in products:
        key = p["product_key"]
        gtin = p.get("gtin")
        if gtin and gtin in by_gtin:
            alias[key] = by_gtin[gtin]
            continue
        if key in canonical:
            continue
        canonical[key] = p
        alias[key] = key
        if gtin:
            by_gtin[gtin] = key

    slugs, tokens, postings = build_index(canonical)
    # Exact-slug lookup handles the easy majority before any fuzzy work.
    exact = {}
    for k, sl in slugs.items():
        exact.setdefault(sl, k)

    # --- 2. attach reviews ----------------------------------------------------
    linked = 0
    queue = []
    for rv in reviews:
        if rv.get("subject_type") == "retailer":
            continue                    # service reviews attach to the retailer, not a bottle
        name_raw = rv.get("product_name_raw")
        if not name_raw:
            queue.append({**rv, "match_reason": "no product name on review"})
            continue
        direct = exact.get(review_slug(name_raw))
        if direct:
            key, score = direct, 1.0
        else:
            key, score = best_match(name_raw, slugs, tokens, postings)
        if key and score >= FUZZY_THRESHOLD:
            rv["product_key"] = key
            rv["match_score"] = score
            linked += 1
        else:
            queue.append({**rv, "match_candidate": key, "match_score": score,
                          "match_reason": "below threshold"})

    prod_reviews = [r for r in reviews if r.get("subject_type") != "retailer"]
    pct = (linked * 100 // len(prod_reviews)) if prod_reviews else 0
    print(f"linked {linked}/{len(prod_reviews)} product reviews ({pct}%); "
          f"{len(queue)} queued for manual review")

    # --- 3. export nested catalogue ------------------------------------------
    offers_by: dict[str, list] = defaultdict(list)
    for o in offers:
        offers_by[alias.get(o["product_key"], o["product_key"])].append(o)
    reviews_by: dict[str, list] = defaultdict(list)
    for rv in reviews:
        if rv.get("product_key"):
            reviews_by[alias.get(rv["product_key"], rv["product_key"])].append(rv)

    catalogue = []
    for key, p in canonical.items():
        offs = offers_by.get(key, [])
        prices = [o["price_aud"] for o in offs if o.get("price_aud") is not None]
        revs = reviews_by.get(key, [])
        scores = [r["rating_norm"] for r in revs if r.get("rating_norm") is not None]
        # Prefer the retailer's published aggregate over an average of our sample.
        summary = None
        for o in offs:
            summary = by_url.get((o.get("url") or "").split("?")[0])
            if summary:
                break
        # Reviews with no recorded sort order predate the sort fix and were
        # captured under the site default ("Highest rating"), so treat them as
        # biased too rather than silently trusting them.
        biased = [r for r in revs if r.get("sort_order") != "Newest"]
        catalogue.append({
            **{k: v for k, v in p.items() if k != "record_type"},
            "offers": [{k: v for k, v in o.items() if k != "record_type"} for o in offs],
            "reviews": [{k: v for k, v in r.items() if k != "record_type"} for r in revs],
            "min_price_aud": min(prices) if prices else None,
            "max_price_aud": max(prices) if prices else None,
            "retailer_count": len({o["retailer"] for o in offs}),
            "review_count": len(revs),
            "avg_rating_norm": round(sum(scores) / len(scores), 4) if scores else None,
            # Site-published aggregate; trust this over avg_rating_norm.
            "site_rating_norm": (summary or {}).get("average_rating_norm"),
            "site_rating": (summary or {}).get("average_rating"),
            "site_review_total": (summary or {}).get("total_reviews"),
            "site_pct_recommend": (summary or {}).get("pct_recommend"),
            "rating_distribution": (summary or {}).get("distribution"),
            "biased_sample": bool(biased),
        })

    catalogue.sort(key=lambda x: (x.get("brand") or "", x["name"]))
    (OUT / "catalogue.json").write_text(
        json.dumps(catalogue, ensure_ascii=False, indent=2), encoding="utf-8")
    with (OUT / "review_queue.jsonl").open("w", encoding="utf-8") as f:
        for q in queue:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")

    multi = sum(1 for c in catalogue if c["retailer_count"] > 1)
    biased = sum(1 for c in catalogue if c.get("biased_sample"))
    with_site = sum(1 for c in catalogue if c.get("site_rating_norm") is not None)
    print(f"-> data/out/catalogue.json  ({len(catalogue)} products, "
          f"{multi} listed by more than one retailer, "
          f"{with_site} with site aggregate rating)")
    if biased:
        print(f"   WARNING: {biased} products carry reviews captured in a biased "
              f"sort order; re-sweep them for a representative sample")
    print(f"-> data/out/review_queue.jsonl  ({len(queue)} rows)")


if __name__ == "__main__":
    main()
