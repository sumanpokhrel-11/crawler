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


def _comparable(offs: list[dict]) -> dict:
    """Like-for-like price comparison: same pack size, different retailers."""
    by_pack: dict[int, dict[str, float]] = {}
    for o in offs:
        p, price, ret = o.get("pack_size"), o.get("price_aud"), o.get("retailer")
        if not p or price is None or not ret:
            continue
        cheapest = by_pack.setdefault(p, {})
        if ret not in cheapest or price < cheapest[ret]:
            cheapest[ret] = price          # a retailer's best price for that pack

    best = None
    for pack, byret in by_pack.items():
        if len(byret) < 2:
            continue
        lo, hi = min(byret.values()), max(byret.values())
        if not lo:
            continue
        spread = round((hi - lo) / lo * 100, 1)
        if best is None or spread > best["compare_spread_pct"]:
            best = {"compare_pack_size": pack,
                    "compare_prices": byret,
                    "compare_spread_pct": spread,
                    "compare_cheapest": min(byret, key=byret.get),
                    # Dan Murphy's API reports Unit "Each" for a case as well as
                    # a bottle, so a case can masquerade as pack_size 1. Real
                    # retail competition does not produce a 2.5x gap on an
                    # identical product; treat those as an unresolved pack-size
                    # mismatch rather than a saving.
                    "compare_suspect": spread > 150}
    if best:
        best["compare_basis"] = "same_pack"
        return best

    # No shared pack size, but every pack size is known — so a per-unit
    # comparison is still sound (a single can at $8.00 vs a 4-pack at $22.99 is
    # $8.00 vs $5.75 each). Only fall back when nothing is guessed.
    per_unit: dict[str, float] = {}
    packs_seen: set[int] = set()
    for o in offs:
        p, price, ret = o.get("pack_size"), o.get("price_aud"), o.get("retailer")
        if not p or price is None or not ret:
            continue
        packs_seen.add(p)
        unit = round(price / p, 2)
        if ret not in per_unit or unit < per_unit[ret]:
            per_unit[ret] = unit
    if len(per_unit) > 1 and len(packs_seen) > 1:
        lo, hi = min(per_unit.values()), max(per_unit.values())
        if lo:
            spread = round((hi - lo) / lo * 100, 1)
            return {"compare_pack_size": None,
                    "compare_prices": per_unit,
                    "compare_spread_pct": spread,
                    "compare_cheapest": min(per_unit, key=per_unit.get),
                    "compare_suspect": spread > 150,
                    "compare_basis": "unit_price"}

    return {"compare_pack_size": None, "compare_prices": {},
            "compare_spread_pct": None, "compare_cheapest": None,
            "compare_suspect": False, "compare_basis": None}


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

    # --- 1b. merge across retailers where only the volume differs ------------
    # Dan Murphy's often omits the bottle size from its listing name (only 636 of
    # 1,526 carry a volume) while Liquorland almost always includes it, so the
    # same bottle hashes to two different keys and never matches. Merge entries
    # whose names agree once volume is set aside — but ONLY when one side's
    # volume is unknown. If both are known and differ they are genuinely
    # different products (375mL vs 700mL), and if a group holds more than one
    # known volume the unknown one is ambiguous, so leave it alone.
    groups: dict[str, list[str]] = defaultdict(list)
    for k, p in canonical.items():
        groups[N.match_slug(p.get("brand"), p["name"], None, p.get("vintage"))].append(k)

    merged = 0
    for _slug, keys in groups.items():
        if len(keys) < 2:
            continue
        known = {canonical[k].get("volume_ml") for k in keys if canonical[k].get("volume_ml")}
        if len(known) != 1:
            continue                       # ambiguous, or all unknown
        vol = known.pop()
        primary = next(k for k in keys if canonical[k].get("volume_ml") == vol)
        for k in keys:
            if k == primary or canonical[k].get("volume_ml"):
                continue
            alias[k] = primary
            canonical.pop(k, None)
            merged += 1
    if merged:
        print(f"merged {merged} products that differed only by a missing volume")

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
        # A one-digit vintage difference barely moves a similarity score, so a
        # 2022 tasting note can attach to the 2000 of the same wine. For wine the
        # vintage IS the product: reject when both sides state one and disagree.
        if key:
            rv_vintage = N.parse_vintage(name_raw)
            pr_vintage = canonical[key].get("vintage") if key in canonical else None
            if rv_vintage and pr_vintage and rv_vintage != pr_vintage:
                key, score = None, 0.0

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
    # De-duplicate offers on the way in. The collector's dedupe is in-memory, so
    # every restart re-admits offers it has already written; doing it here makes
    # the result independent of how many times the collector was restarted.
    # One row per product/retailer/price/day - a real price change still creates
    # a new row, so the price time series survives.
    # Key on price and day only: a repeat visit sometimes captures the member
    # price and sometimes does not, and those are the same offer, not two.
    # Keep whichever row carries the most information.
    best: dict[tuple, dict] = {}
    order: list[tuple] = []
    dupes = 0
    for o in offers:
        key = alias.get(o["product_key"], o["product_key"])
        sig = (key, o.get("retailer"), o.get("price_aud"), (o.get("scraped_at") or "")[:10])
        prev = best.get(sig)
        if prev is None:
            best[sig] = o
            order.append(sig)
            continue
        dupes += 1
        score = lambda x: sum(1 for f in ("member_price_aud", "promo", "in_stock",
                                          "aggregate_review_count", "aggregate_rating_norm")
                              if x.get(f) is not None)
        if score(o) > score(prev):
            best[sig] = o

    offers_by: dict[str, list] = defaultdict(list)
    for sig in order:
        offers_by[sig[0]].append(best[sig])
    if dupes:
        print(f"dropped {dupes} duplicate offer rows (collector restarts)")
    reviews_by: dict[str, list] = defaultdict(list)
    for rv in reviews:
        if rv.get("product_key"):
            reviews_by[alias.get(rv["product_key"], rv["product_key"])].append(rv)

    catalogue = []
    for key, p in canonical.items():
        offs = offers_by.get(key, [])
        prices = [o["price_aud"] for o in offs if o.get("price_aud") is not None]
        units = [o["unit_price_aud"] for o in offs if o.get("unit_price_aud") is not None]
        packs = {o.get("pack_size") for o in offs if o.get("pack_size")}
        revs = reviews_by.get(key, [])
        scores = [r["rating_norm"] for r in revs if r.get("rating_norm") is not None]
        # Prefer the retailer's published aggregate over an average of our sample.
        summary = None
        for o in offs:
            summary = by_url.get((o.get("url") or "").split("?")[0])
            if summary:
                break
        # Flag only a *known* bad sort order. Dan Murphy's defaults to "Highest
        # rating", which over-samples positive reviews, so anything not captured
        # under "Newest" there is suspect. Liquorland offers no sort control at
        # all (sort_order is null) and its JSON-LD sample tested unbiased against
        # the site's published averages - median +0.000 over 2,071 products - so
        # a null sort order is "not applicable", not "biased".
        biased = [r for r in revs
                  if r.get("sort_order") is not None and r["sort_order"] != "Newest"]
        catalogue.append({
            **{k: v for k, v in p.items() if k != "record_type"},
            "offers": [{k: v for k, v in o.items() if k != "record_type"} for o in offs],
            "reviews": [{k: v for k, v in r.items() if k != "record_type"} for r in revs],
            "min_price_aud": min(prices) if prices else None,
            "max_price_aud": max(prices) if prices else None,
            # Retailers sell the same drink as a single, a 6-pack and a case, and
            # pack size is not part of product identity - so comparing headline
            # prices reports a 6-pack against a single can as a 520% "saving".
            # Unit price is the only sound basis for comparison.
            "pack_sizes": sorted(packs) if packs else [],
            "pack_size_mismatch": len(packs) > 1,
            # Compare only offers that agree on pack size AND come from different
            # retailers. Dan Murphy's API reports Unit "Each" even for a case, so
            # a case can look like a single bottle - unit price is not reliable
            # enough on its own to normalise across pack sizes.
            **_comparable(offs),
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
    trusted = [c for c in catalogue
               if c.get("compare_spread_pct") is not None and not c.get("compare_suspect")]
    print(f"   like-for-like comparisons: {len(trusted)} trusted, "
          f"{sum(1 for c in catalogue if c.get('compare_suspect'))} flagged as likely pack mismatch")
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
