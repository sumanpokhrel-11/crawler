"""Winepilot — HTTP lane via the WordPress REST API.

Winepilot is a WordPress site whose reviews are ordinary posts, exposed at
/wp-json/wp/v2/posts with a custom `points` field carrying the 100-point score.
No browser, no extension: 16k reviews in ~160 paginated requests.

    python3 scrapers/winepilot_lane.py [--limit-pages N]
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
API = "https://winepilot.com/wp-json/wp/v2/posts"
SOURCE = "winepilot"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
TAGS = re.compile(r"<[^>]+>")


def get(url: str, timeout: int = 40):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        # Header case varies by server; normalise so X-WP-Total is always found.
        data = r.read()
        headers = {k.lower(): v for k, v in r.headers.items()}
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return json.loads(data.decode("utf-8", "ignore")), headers


def clean(fragment: str | None) -> str:
    """WP returns rendered HTML; reviews are plain prose underneath."""
    if not fragment:
        return ""
    text = TAGS.sub(" ", fragment)
    return re.sub(r"\s{2,}", " ", html_mod.unescape(text)).strip()


def to_review(post: dict) -> dict | None:
    title = clean((post.get("title") or {}).get("rendered"))
    body = clean((post.get("content") or {}).get("rendered"))
    if not title or len(body) < 40:
        return None                      # not a tasting note

    # `points` is Winepilot's 100-point score, matching Halliday's scale, so it
    # normalises the same way.
    raw = post.get("points")
    try:
        points = float(str(raw).strip()) if raw not in (None, "") else None
    except ValueError:
        points = None

    return {
        "record_type": "review",
        "review_key": N.review_key(SOURCE, post.get("link") or "", None, body),
        "product_key": None,             # resolve.py links it to a catalogue product
        "product_name_raw": title,
        "subject_type": "product",
        "source": SOURCE,
        "source_url": post.get("link"),
        "author": None,                  # house reviews, no per-note byline
        "title": title,
        "text": body,
        "rating_raw": points,
        "rating_scale": 100 if points is not None else None,
        "rating_norm": N.norm_rating(points, 100),
        "review_date": (post.get("date") or "")[:10] or None,
        "sort_order": "Newest",          # API returns newest first, unfiltered
        "scraped_at": N.now_iso(),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-pages", type=int, default=0)
    ap.add_argument("--per-page", type=int, default=100)
    args = ap.parse_args()

    _, headers = get(f"{API}?per_page=1")
    total = int(headers.get("x-wp-total", 0))
    pages = int(headers.get("x-wp-totalpages", 1))
    per = args.per_page
    pages = -(-total // per) if total else pages
    if args.limit_pages:
        pages = min(pages, args.limit_pages)
    print(f"{total} posts, fetching {pages} pages of {per}")

    out_path = OUT / "winepilot.jsonl"
    n, scored, skipped = 0, 0, 0
    with out_path.open("w", encoding="utf-8") as fh:
        for page in range(1, pages + 1):
            try:
                posts, _ = get(f"{API}?per_page={per}&page={page}")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                print(f"  ! page {page}: {e}")
                break
            for post in posts:
                rec = to_review(post)
                if not rec:
                    skipped += 1
                    continue
                n += 1
                scored += rec["rating_raw"] is not None
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if page % 20 == 0 or page == pages:
                print(f"  page {page}/{pages}: {n} reviews ({scored} scored)")
            time.sleep(0.4 + random.uniform(0, 0.4))

    print(f"done: {n} reviews, {scored} with a score, {skipped} skipped "
          f"-> {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
