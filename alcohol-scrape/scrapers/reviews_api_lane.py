"""Dan Murphy's and BWS reviews over HTTP, via Endeavour's BazaarVoice proxy.

Both storefronts render reviews client-side, which is why the first pass used
the browser extension: one tab per product, click "next page", wait. That run
took thirteen hours and truncated long reviews at 250 characters (the DOM holds
only what the "Read more" toggle has expanded).

The widget is fed by an endpoint that answers plain HTTP:

    GET {api}/apis/ui/BazaarVoice/RatingsAndReviews
        ?ProductId=<id>&Sort=SubmissionTime:desc&PageOffset=<n>&PageSize=100

The two sites are the same platform at different versions, so the request and
the response differ:

  Dan Murphy's  ProductId "DM_73796" (prefixed)   -> {"Reviews": [...],
                                                      "RatingDistribution": [...]}
  BWS           ProductId "122063"  (bare)        -> {"reviews": [...],
                                                      "overall": {...}}

Three things matter here beyond speed:

  * `Sort=SubmissionTime:desc` is newest-first. The page itself defaults to
    `Rating:desc`, which is what poisoned the first review sweep: a product
    averaging 1.6 stars showed four 5-star reviews at the top, and 99% of the
    first 2,000 reviews collected were 5-star.
  * `LongDesc` / `description` is the full review body, so the "Read more"
    truncation disappears.
  * `RatingDistribution` / `overall` gives the site's own published star
    distribution in the same call, which is the ground truth a sampled set gets
    checked against.

Depth follows the agreed sampling rule, overridable once the client answers:
200+ reviews -> 50 most recent, 21-199 -> 20, 20 or fewer -> all.

    python3 scrapers/reviews_api_lane.py --source dan_murphys
    python3 scrapers/reviews_api_lane.py --source bws --full
"""
from __future__ import annotations

import argparse
import gzip
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import normalize as N

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "out"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
PAGE_SIZE = 100          # 200 is accepted but silently returns nothing
FAILURE_LIMIT = 10       # consecutive product failures before giving up

SITES = {
    "dan_murphys": {
        "api": "https://api.danmurphys.com.au",
        "site": "https://www.danmurphys.com.au",
        "prefix": "DM_",
        "product_url": "{site}/product/DM_{code}/",
    },
    "bws": {
        "api": "https://api.bws.com.au",
        "site": "https://bws.com.au",
        "prefix": "",
        "product_url": "{site}/product/{code}/",
    },
}


def depth_for(total: int, full: bool, cap: int | None) -> int:
    """How many of the most recent reviews to keep for a product."""
    if full:
        return total
    if cap:
        return min(total, cap)
    if total >= 200:
        return 50
    if total > 20:
        return 20
    return total


def get(url: str, referer: str, timeout: int = 45, tries: int = 5):
    delay = 5.0
    headers = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
               "Referer": referer, "Accept-Encoding": "gzip"}
    headers["Origin"] = referer.split("/product/")[0]
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return json.loads(data.decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            # 403 is what BWS returns when the request arrives from an IP
            # Cloudflare does not like, which is exactly what a dropped VPN
            # looks like. Treat it as transient here and let the caller's
            # circuit breaker decide whether the drop is permanent.
            if e.code in (403, 429, 503) and attempt < tries - 1:
                label = "blocked" if e.code == 403 else "throttled"
                print(f"    {label} ({e.code}), waiting {delay:.0f}s")
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            if attempt == tries - 1:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def parse_date(value) -> str | None:
    """Dan Murphy's sends "29 August 2026"; BWS sends ISO-8601."""
    raw = (value or "").strip()
    if not raw:
        return None
    for fmt in ("%d %B %Y", "%d %b %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    try:                                  # BWS sends ISO-8601 with an offset
        return datetime.fromisoformat(raw).date().isoformat()
    except ValueError:
        return None


def page(source: str, code: str, offset: int) -> dict:
    cfg = SITES[source]
    params = urllib.parse.urlencode({
        "ProductId": cfg["prefix"] + code,
        "Sort": "SubmissionTime:desc",
        "PageOffset": offset,
        "PageSize": PAGE_SIZE,
        "ShopperId": "",
    })
    url = f"{cfg['api']}/apis/ui/BazaarVoice/RatingsAndReviews?{params}"
    return get(url, cfg["product_url"].format(site=cfg["site"], code=code))


def reviews_of(payload: dict) -> list[dict]:
    return payload.get("Reviews") or payload.get("reviews") or []


def to_review(source: str, url: str, product_name: str | None, r: dict) -> dict | None:
    text = (r.get("LongDesc") or r.get("description") or "").strip()
    if len(text) < 15:            # review text is the deliverable
        return None
    author = r.get("Name") or r.get("displayName")
    rating = r.get("Rating") if r.get("Rating") is not None else r.get("rating")
    return {
        "record_type": "review",
        "review_key": N.review_key(source, url, author, text),
        "product_key": None,
        "product_name_raw": product_name,
        "subject_type": "product",
        "source": source,
        "source_url": url,
        "author": author,
        "title": r.get("Title") or r.get("title"),
        "text": text,
        "rating_raw": rating,
        "rating_scale": 5,
        "rating_norm": N.norm_rating(rating, 5),
        "sort_order": "Newest",
        "review_date": parse_date(r.get("DateSubmitted") or r.get("date")),
        "scraped_at": N.now_iso(),
        "verified_purchase": r.get("IsRecommended") if "IsRecommended" in r
                             else r.get("wouldRecommend"),
        "helpful_votes": r.get("Likes") if r.get("Likes") is not None
                         else ((r.get("helpful") or {}).get("yes")
                               if isinstance(r.get("helpful"), dict) else None),
        "syndicated_from": r.get("SyndReviewSource") or r.get("SourceClient"),
    }


def to_summary(source: str, url: str, product_name: str | None, payload: dict) -> dict | None:
    """Both shapes carry the site's own published star distribution."""
    dist, avg, total, pct = {}, None, None, None
    if payload.get("RatingDistribution"):
        for row in payload["RatingDistribution"]:
            dist[str(row.get("RatingValue"))] = row.get("Count")
        avg = payload.get("OverallRating")
        total = payload.get("TotalReviewCount") or payload.get("TotalResults")
        pct = payload.get("RecommendedPercentage")
    elif payload.get("overall"):
        o = payload["overall"]
        avg = o.get("rating")
        total = o.get("totalReviews")
        pct = N.parse_price(str(o.get("percentageRecommending") or "").rstrip("%"))
    if avg is None and not dist:
        return None
    return {
        "record_type": "review_summary",
        "product_name_raw": product_name,
        "source": source,
        "source_url": url,
        "average_rating": round(avg, 4) if isinstance(avg, (int, float)) else None,
        "rating_scale": 5,
        "average_rating_norm": N.norm_rating(avg, 5),
        "pct_recommend": pct,
        "distribution": dist or None,
        "total_reviews": total,
        "scraped_at": N.now_iso(),
    }


def scan_targets(files: list[str], pattern: str) -> list[tuple[str, int, str | None]]:
    """(stockcode, published review count, product name) from our own catalogue.

    The reviews payload names the product on BWS but not on Dan Murphy's, and
    entity resolution matches reviews to products by name, so the name is read
    back out of the records the listing crawl already wrote.
    """
    counts: dict[str, int] = {}
    names: dict[str, str] = {}
    urls: dict[str, str] = {}
    for path in files:
        f = OUT / path
        if not f.exists():
            continue
        for line in f.open(encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            kind = r.get("record_type")
            if kind not in ("offer", "product"):
                continue
            m = re.search(pattern, r.get("url") or r.get("source_url") or "")
            if not m:
                continue
            code = m.group(1)
            if kind == "offer":
                n = r.get("aggregate_review_count") or 0
                if n:
                    counts[code] = max(counts.get(code, 0), int(n))
                    urls.setdefault(code, r.get("url") or "")
            elif r.get("name"):
                names.setdefault(code, r["name"])
    return sorted(((c, n, names.get(c)) for c, n in counts.items()),
                  key=lambda t: -t[1])


def targets_dan_murphys():
    return scan_targets(["extension_products.jsonl"], r"/product/DM_(\d+)/")


def targets_bws():
    if not (OUT / "bws.jsonl").exists():
        raise SystemExit("run scrapers/bws_lane.py first (data/out/bws.jsonl missing)")
    return scan_targets(["bws.jsonl"], r"/product/(\d+)/")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=sorted(SITES), required=True)
    ap.add_argument("--full", action="store_true",
                    help="every review, ignoring the sampling rule")
    ap.add_argument("--cap", type=int, default=0, help="fixed per-product cap")
    ap.add_argument("--limit", type=int, default=0, help="stop after N products")
    ap.add_argument("--delay", type=float, default=1.0)
    args = ap.parse_args()

    source = args.source
    cfg = SITES[source]
    targets = targets_dan_murphys() if source == "dan_murphys" else targets_bws()
    if args.limit:
        targets = targets[:args.limit]
    print(f"{source}: {len(targets)} products with reviews, "
          f"{sum(t[1] for t in targets):,} reviews published")

    # Resume across runs: a throttle or a laptop lid should not restart the job.
    state_path = OUT / f"reviews_api_{source}_state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"done": []}
    done = set(state["done"])

    rev_path = OUT / f"reviews_api_{source}.jsonl"
    sum_path = OUT / f"reviews_api_{source}_summaries.jsonl"
    seen_keys: set[str] = set()
    if rev_path.exists():
        for line in rev_path.open(encoding="utf-8"):
            try:
                seen_keys.add(json.loads(line)["review_key"])
            except (ValueError, KeyError):
                pass

    kept = 0
    # A lost VPN or a hard block fails every product identically. Without a
    # stop, the run would chew through thousands of targets in minutes,
    # collect nothing and still print "done".
    consecutive_failures = 0
    aborted = False
    with rev_path.open("a", encoding="utf-8") as rf, \
         sum_path.open("a", encoding="utf-8") as sf:
        for idx, (code, published, known_name) in enumerate(targets, 1):
            if code in done:
                continue
            url = cfg["product_url"].format(site=cfg["site"], code=code)
            want = depth_for(published, args.full, args.cap or None)
            name, offset, got = known_name, 0, 0
            try:
                while got < want:
                    payload = page(source, code, offset)
                    if offset == 0:
                        name = payload.get("productTitle") or known_name
                        s = to_summary(source, url, name, payload)
                        if s:
                            sf.write(json.dumps(s, ensure_ascii=False) + "\n")
                    batch = reviews_of(payload)
                    if not batch:
                        break
                    for raw in batch:
                        if got >= want:
                            break
                        rec = to_review(source, url, name, raw)
                        if not rec or rec["review_key"] in seen_keys:
                            continue
                        seen_keys.add(rec["review_key"])
                        rf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        got += 1
                        kept += 1
                    if len(batch) < PAGE_SIZE:
                        break
                    offset += PAGE_SIZE
                    time.sleep(args.delay + random.uniform(0, 0.4))
            except (urllib.error.HTTPError, urllib.error.URLError,
                    TimeoutError, OSError, ValueError) as e:
                consecutive_failures += 1
                print(f"  ! {code} failed: {e}")
                if consecutive_failures >= FAILURE_LIMIT:
                    print(f"\n  ABORTED after {FAILURE_LIMIT} consecutive "
                          f"failures — the source is unreachable (VPN down?).\n"
                          f"  Progress is saved; re-run the same command to "
                          f"resume from here.")
                    aborted = True
                    break
                continue

            consecutive_failures = 0
            done.add(code)
            if idx % 25 == 0 or got:
                print(f"  [{idx}/{len(targets)}] {code}: +{got} "
                      f"(published {published}) | total {kept}")
            if idx % 50 == 0:
                rf.flush(); sf.flush()
                state_path.write_text(json.dumps({"done": sorted(done)}))
            time.sleep(args.delay + random.uniform(0, 0.4))

    state_path.write_text(json.dumps({"done": sorted(done)}))
    verb = "stopped early" if aborted else "done"
    print(f"{verb}: {kept} reviews, {len(done)}/{len(targets)} products "
          f"-> {rev_path.relative_to(ROOT)}")
    if aborted:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
