"""Build the listing-crawl queue from config/listing_seeds.txt.

The product catalogue previously came from whatever pages happened to be visited.
A seed list makes coverage explicit and repeatable: edit the file, rebuild, crawl.

    python3 scrapers/build_listing_queue.py [--clicks 60]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEEDS = ROOT / "config" / "listing_seeds.txt"
TARGETS = ROOT / "data" / "out" / "listing_targets.json"
STATE = ROOT / "data" / "out" / "listing_state.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clicks", type=int, default=60,
                    help='"Show 24 more" clicks per page; 60 ~= 1500 products')
    ap.add_argument("--keep-progress", action="store_true",
                    help="do not reset progress for pages already crawled")
    args = ap.parse_args()

    if not SEEDS.exists():
        print(f"no seed file at {SEEDS}")
        return

    # A seed may be "URL" or "URL | pages=N". Dan Murphy's paginates with a
    # "Show 24 more" button (handled by clicks); Liquorland paginates by ?page=N,
    # so those seeds are expanded into one target per page at build time.
    queue, seen = [], set()
    for line in SEEDS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        pages = 0
        if "|" in line:
            line, _, opts = line.partition("|")
            line = line.strip()
            for part in opts.split():
                if part.startswith("pages="):
                    try:
                        pages = int(part.split("=", 1)[1])
                    except ValueError:
                        pages = 0
        base = line.split("?")[0]
        if pages > 0:
            for n in range(1, pages + 1):
                u = base if n == 1 else f"{base}?page={n}"
                if u not in seen:
                    seen.add(u)
                    queue.append({"url": u, "clicks": 0})
        elif base not in seen:
            seen.add(base)
            queue.append({"url": base, "clicks": args.clicks})
    TARGETS.parent.mkdir(parents=True, exist_ok=True)
    TARGETS.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    if STATE.exists() and not args.keep_progress:
        STATE.unlink()

    paged = sum(1 for q in queue if not q["clicks"])
    print(f"queued {len(queue)} listing pages "
          f"({len(queue) - paged} click-paginated, {paged} url-paginated)")
    print(f"-> {TARGETS.relative_to(ROOT)}")
    for q in queue[:5]:
        print(f"   {q['url']}")
    if len(queue) > 5:
        print(f"   ... and {len(queue) - 5} more")


if __name__ == "__main__":
    main()
