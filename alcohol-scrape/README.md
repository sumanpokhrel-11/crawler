# Alcohol Scrape

Products, prices and review text from Australian alcohol retailers, joined into a
single JSON catalogue for price comparison.

Built for a client brief covering AU bottle shops (Dan Murphy's, BWS, Liquorland,
Thirsty Camel) and independents (Nicks, Kent Street Cellars, Liquor Loot,
MyBottleShop), plus review sources.

## Current state

| | products | offers | reviews |
|---|---|---|---|
| Liquorland | 8,884 | 8,884 | **5,200** (~3/product ceiling) |
| Kent Street Cellars | 7,818 | 7,818 | none (no adapter) |
| Dan Murphy's | 4,659 | 4,659 | **10,872** |
| **catalogue total** | **18,438** | **18,261** | **16,029** |

15,231 products carry a price and 3,882 carry the retailer's published rating.
**209 products match across more than one retailer** — that set is what the
comparison is actually built on. Median price gap between retailers on those is 11.1%.

Both review samples test unbiased against the retailers' own published star
distributions: Dan Murphy's median +0.000 (n=471), Liquorland median +0.000
(n=2,071). That comparison is the standing check on whether a sample is honest.

## Why two lanes

An audit of every source in the brief found roughly half unreachable from a server:

| Source | Lane | Status |
|---|---|---|
| Kent Street Cellars | HTTP | Shopify `/products.json` — 7,818 products |
| Dan Murphy's | extension | Cloudflare-blocked server-side; works in-browser |
| Liquorland | extension | ShieldSquare blocks servers; works in-browser |
| BWS | **blocked** | Cloudflare hard-block ("you have been blocked"), in a real browser too |
| Thirsty Camel, Nicks | extension | JS-rendered, no product endpoint |
| Liquor Loot, MyBottleShop | — | connection timeout (likely AU geo-fence) |
| Untappd, Crafty Pint | extension | Cloudflare |
| Trustpilot AU, ProductReview | extension | robots.txt disallows crawlers |

- **HTTP lane** (`scrapers/http_lane.py`) — plain requests, for sources that answer.
- **Extension lane** (`extension/`) — Chrome MV3 extension that extracts from pages in
  a real browser session and posts to a local collector. This is the primary path,
  not a fallback.

Both converge on `scrapers/normalize.py`, so records are identical in shape whichever
lane produced them. The extension deliberately emits **raw strings** and lets Python
parse them — no duplicated parsing logic in JS to drift out of sync.

## Quick start

```bash
python3 scrapers/collector.py          # terminal 1: queue server + ingest
# chrome://extensions -> Developer mode -> Load unpacked -> ./extension
# reload any tab that was already open (content scripts only inject on page load)
```

Then from the extension popup:
- **Start listing crawl (seeded)** — products + prices from `config/listing_seeds.txt`
- **Start review sweep** — review text from product pages
- **Worker tabs** (1–4) runs several products at once

```bash
python3 scrapers/resolve.py            # -> data/out/catalogue.json
```

`./start_sweep.sh` launches the collector, `caffeinate` and a progress watcher
together for unattended runs. Don't start the collector separately as well — they
collide on port 8765.

No third-party dependencies: stdlib only, Python 3.9+.

## Pipeline

```
config/listing_seeds.txt
   -> build_listing_queue.py  -> listing_targets.json
        -> [extension listing crawl] -> collector -> extension_products.jsonl
             -> resolve.py -> catalogue.json
                  -> build_queue.py -> review_targets.json
                       -> [extension review sweep] -> collector -> extension_reviews.jsonl
                            -> resolve.py -> catalogue.json (reviews attached)
```

| Script | Does |
|---|---|
| `collector.py` | Local HTTP server: serves both queues, normalises and writes records |
| `normalize.py` | Volume, pack size, ABV, vintage, category, price, name, match keys |
| `http_lane.py` | Shopify scraper (Kent Street Cellars) |
| `build_listing_queue.py` | Listing targets from the seed file |
| `build_queue.py` | Review targets ranked from the catalogue |
| `resolve.py` | Entity resolution + nested `catalogue.json` export |
| `reprocess.py` | Replay archived raw batches after a parser change (no re-crawl) |
| `requeue.py` | Reset stalled sweep targets |
| `watch_sweep.py` | Logs progress to `logs/sweep.log`, flags stalls |

## Output

`data/out/catalogue.json` — one object per product with nested `offers[]` and
`reviews[]`, plus `min_price_aud`, `retailer_count`, `review_count`,
`site_rating_norm` and `rating_distribution`. Contract: `schema/records.schema.json`.

Ratings normalise to `rating_norm` 0..1 so Untappd (/5), Trustpilot (/5) and
Halliday (/100) are comparable, with the raw value and scale retained.

`data/raw/` archives every response and batch, so a parser fix replays over collected
data instead of re-crawling. `data/` is gitignored (~156MB).

## Site-specific behaviour

Things that are not obvious and cost real time to discover.

**Dan Murphy's** — Angular, no `data-testid`, no JSON-LD products.
- **Two tile layouts on the same page**: `.title`/`.subtitle` + `.card-price`, and
  `.product__title` + `.product__price-value`. Handling only one loses ~half the prices.
- **Dual pricing**: member and non-member. `price_aud` is the non-member (general
  shopper) price; `member_price_aud` is the loyalty price. Comparing on member
  pricing alone misrepresents what most people pay.
- **Infinite loader**, not pagination: a "Show 24 more" button appends tiles. The
  grid renders well after `document_idle`, so the crawler waits for it before
  looking for the button — searching too early finds nothing and harvests one
  screen (24 of 6771).
- **Review sort defaults to "Highest rating"**, which is not a representative
  sample: a product averaging 1.6 stars showed `5,4,4,4` on page one and `1,1,1,4`
  sorted by Newest. The sweep sets Newest and records `sort_order` on every review.
  Treat any review whose `sort_order` is not `"Newest"` as biased.
- Long reviews render truncated behind a "Read more" toggle whose label leaks into
  the text node. The sweep expands each card first (one review: 240 -> 777 chars).
- Review pagination **replaces** the four visible cards rather than appending, so
  every page is harvested before advancing.

**Liquorland** — 60 `.ProductTileV3` per page, paginated by `?page=N`.
- Prices are split across `.dollarAmount`/`.centsAmount`, and the same markup is
  reused for current / was / saving — scope to `.PriceTagV3.current` or the
  discount gets recorded as the price.
- Reviews exist only in **JSON-LD**; the on-page Bazaarvoice widget renders a star
  summary and never lists reviews. The review objects sit in a `review` array on a
  node with no `@type` and use `headline`/`reviewBody`.
- **Only ~3 reviews per product are exposed**, whatever the advertised count
  (Baileys advertises 6,813 and the page carries 3). The rest stay inside
  Bazaarvoice. Aggregate rating and review count are complete, though.
- The Product JSON-LD node (with `aggregateRating`) renders **before** the review
  array. Waiting on `aggregateRating` harvests too early and returns nothing —
  wait for the review array itself.
- Product JSON-LD also carries `gtin13`, the best cross-retailer match key we have.

## Operating notes

- **Restart `collector.py` after changing any Python.** It is long-running and holds
  the old module in memory — a stale collector silently drops new fields while still
  reporting healthy batch counts.
- **Reload the extension after changing anything under `extension/`**, and reload the
  page too: content scripts only inject on page load.
- Chrome throttles timers in hidden/occluded tabs, so extra workers open in their own
  windows. Keep them on screen and unminimised.
- Background work is scheduled with `chrome.alarms`, not `setTimeout`: Chrome
  terminates the MV3 service worker after ~30s idle and pending timeouts die with it
  (this cost 2h40m on the first overnight run). A watchdog resumes a stalled run.
- The collector hands out one target at a time under an in-flight lock, verified with
  48 concurrent requests and zero duplicates — parallel tabs cannot double-scrape.
- Sweep progress persists; stopping and resuming loses nothing, and `review_key`
  dedupes so re-running is always safe.
- Use 2–3 worker tabs. Each multiplies request rate against one site from one IP.

## Entity resolution

`resolve.py` de-duplicates offers (one row per product/retailer/price/day — the
collector's dedupe is in-memory and resets on restart), then matches on GTIN, exact
slug, and fuzzy slug (0.86 threshold) over an inverted token index. Anything below threshold goes to `data/out/review_queue.jsonl`
for a human rather than being guessed at. Current link rate: **99%**.

Review slugs must be built with volume and vintage parsed from the heading —
otherwise identical products score ~0.81 against the 0.86 cutoff.

## Known gaps

- **BWS** is hard-blocked by Cloudflare; not viable without a different approach.
- **Kent Street Cellars** has 7,818 products and no reviews (Shopify; review source
  not yet identified).
- **Dan Murphy's coverage is capped by clicks**: `/list/wine` advertises 6,771
  products and 60 clicks captured 1,464. Raise `--clicks` to close the gap.
- Liquorland reviews are capped at ~3/product by the site; 79 of 4,934 targets
  failed repeatedly and were skipped.
- No adapters yet for BeerIsOk, Winepilot, Untappd, The Crafty Pint.
- Paid review sites (Halliday, The Real Review, The Wine Front) are subscription
  content — client sign-off needed before building those.
