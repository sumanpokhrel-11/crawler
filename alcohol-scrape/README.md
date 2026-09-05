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
| Dan Murphy's | extension + HTTP | listing crawl in-browser; reviews via the BazaarVoice endpoint |
| BWS | HTTP | sitemap gives every stockcode; Endeavour API gives the rest |
| Liquorland | extension | ShieldSquare blocks servers; works in-browser |
| Thirsty Camel, Nicks | HTTP | backend API / sitemap + JSON-LD |
| Liquor Loot, MyBottleShop | — | connection timeout (likely AU geo-fence) |
| Untappd, Crafty Pint | extension | Cloudflare |
| Trustpilot AU, ProductReview | extension | robots.txt disallows crawlers |

BWS was recorded as a hard block for most of this project and is not: the block is
IP-reputation based, and the same requests succeed from a different egress. It is
still the most aggressively rate-limited source here — see below.

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

The HTTP lanes bypass the queue entirely and write straight into `data/out/`, which
`resolve.py` globs:

```
bws_lane.py         -> bws.jsonl            (sitemap -> stockcodes -> API)
reviews_api_lane.py -> reviews_api_<source>.jsonl + _summaries.jsonl
```

`reviews_api_lane.py` reads its targets out of records already collected (any offer
carrying an `aggregate_review_count`), keeps a resume file per source, and applies
the agreed sampling depth: 200+ published reviews -> 50 most recent, 21-199 -> 20,
20 or fewer -> all. `--full` or `--cap N` overrides it once the client decides.

Both Endeavour lanes and the extension can produce the same review. `resolve.py`
de-duplicates on source plus author plus body, keeping the longer text, because
`review_key` hashes the source URL and the two lanes write different URLs for one
product.

| Script | Does |
|---|---|
| `collector.py` | Local HTTP server: serves both queues, normalises and writes records |
| `normalize.py` | Volume, pack size, ABV, vintage, category, price, name, match keys |
| `http_lane.py` | Shopify scraper (Kent Street Cellars) |
| `bws_lane.py` | BWS: product sitemap -> Endeavour `Products/` API (no browser) |
| `reviews_api_lane.py` | Dan Murphy's + BWS reviews via the BazaarVoice endpoint |
| `nicks_lane.py` | Nicks: sitemap + JSON-LD |
| `thirstycamel_lane.py` | Thirsty Camel: Cloud Run backend API |
| `winepilot_lane.py` | Winepilot tasting notes via the WordPress REST API |
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

**Dan Murphy's has a JSON API — prefer it over scraping the DOM.**

```
POST api.danmurphys.com.au/apis/ui/Browse
     {"department":"spirits","subDepartment":"gin","filters":[],
      "pageNumber":1,"pageSize":100,"sortType":"Relevance","PageUrl":"/spirits/gin"}
GET  api.danmurphys.com.au/apis/ui/Products/<comma-separated stockcodes>
```

Both run from the extension background worker (host permissions cover the
subdomain) — no page, no rendering, no tab throttling. `Products/` returns price,
member price, `PackageSize`, `OverallRating` and `NumberOfReviews` for ~40
stockcodes per call in about 2 seconds, and every product URL carries its
stockcode (`/product/DM_73796/...`). That is what `build_enrich_queue.py` plus the
popup's **Fill prices via API** use to fill prices the DOM crawl missed.

`Browse` needs the site's internal taxonomy: `department`/`subDepartment` must be
real values (`spirits`/`gin` works, `beer`/`all` 404s), so it cannot be derived
from the URL path alone. Capture a real request body from the page to learn them.

**Both Endeavour sites serve their reviews over plain HTTP.**

The review widget on a Dan Murphy's or BWS product page is fed by:

```
GET {api}/apis/ui/BazaarVoice/RatingsAndReviews
    ?ProductId=<id>&Sort=SubmissionTime:desc&PageOffset=<n>&PageSize=100
```

`scrapers/reviews_api_lane.py` drives it. This replaced the browser review sweep,
which took thirteen hours for one site. Four things it fixes:

- **No truncation.** `LongDesc` is the whole review body. The DOM holds only what
  the "Read more" toggle has expanded, which cut 206 reviews at exactly 250
  characters — one grew from 240 to 777 characters when expanded.
- **No sampling bias.** `Sort=SubmissionTime:desc` is newest-first. The page
  itself defaults to `Rating:desc`, which is what poisoned the first sweep: 99% of
  the first 2,000 reviews collected were 5-star, and a product averaging 1.6 stars
  presented four 5-star reviews at the top.
- **Ground truth in the same call.** `RatingDistribution` (Dan Murphy's) and
  `overall` (BWS) return the site's own published star distribution, which is what
  a sampled set gets checked against.
- **100 reviews per request** with `PageOffset` paging, verified to the end of a
  6,936-review product. `PageSize=200` is accepted and silently returns nothing.

The two sites are the same platform at different versions, so they differ:

| | Dan Murphy's | BWS |
|---|---|---|
| `ProductId` | `DM_73796` (prefixed) | `122063` (bare) |
| reviews key | `Reviews` | `reviews` |
| body field | `LongDesc` | `description` |
| date format | `29 August 2026` | ISO-8601 |
| product name | absent, read from our catalogue | `productTitle` |

**BWS needs no browser at all.**

`robots.txt` publishes `https://bws.com.au/sitemap/products-sitemap.xml`, and every
URL in it carries the stockcode: `/product/<stockcode>/<slug>`. That is the whole
catalogue enumeration with no category crawl. Feed those codes to
`api.bws.com.au/apis/ui/Products/<code,code,...>` (30 per call) and it returns name,
brand, price, was-price, package size, availability, rating and review count, plus
an `AdditionalDetails` list carrying ABV, country, state, region, vintage, varietal
and standard drinks — richer product metadata than any other source here.

Two traps in that payload: some `AdditionalDetails` values are lists, not strings
(`categorynodename`), and `image1` is a bare filename that has to be prefixed with
`https://egl-assets.scene7.com/is/image/endeavour/` with the extension stripped.

Cloudflare rate-limits BWS hard. A 200-request discovery burst at concurrency 8
earned an immediate 429 challenge that took a minute to clear, so both BWS lanes
run serially with a delay and back off exponentially on 429/503.

The category tree is walkable if it is ever needed (`POST /apis/ui/Browse` with
`{"CategoryId": "<id>", "PageNumber": 1, "PageSize": 100, "Filters": [],
"SortType": "TopSellers"}`): each response names the node, its `ParentId` and its
children, roots have `ParentId` 0, and `All Wine` is 1107, `All Spirits` 1109. The
sitemap makes it unnecessary.

The DOM notes below still apply to the listing crawl:

**Dan Murphy's** — Angular, no `data-testid`, no JSON-LD products.
- **Two tile layouts on the same page**: `.title`/`.subtitle` + `.card-price`, and
  `.product__title` + `.product__price-value`. Handling only one loses ~half the prices.
- **Dual pricing**: member and non-member. `price_aud` is the non-member (general
  shopper) price; `member_price_aud` is the loyalty price. Comparing on member
  pricing alone misrepresents what most people pay.
- **Listing pages only render prices near the viewport.** After a 200-click deep
  crawl, 78% of tiles had a name and image but no price. The crawler now harvests
  after every click (while the new batch is on screen) and keeps whichever version
  of a tile carries a price — but the API above is the reliable fix.
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

## Review sources

| Source | Lane | Reviews | Notes |
|---|---|---|---|
| Dan Murphy's | extension | 10,835 | must sort by Newest — default is biased |
| Liquorland | extension | 5,183 | JSON-LD only, ~3 per product ceiling |
| **Winepilot** | **HTTP (WP REST API)** | **16,003 collected** | 100-point scores, full tasting notes |
| BeerIsOk | — | 9 total | not worth crawling |
| **The Crafty Pint** | **extension (same-origin fetch)** | **3,702** | craft beer; ~2,270 chars each |
| Untappd | browser | — | accessible, but user-generated content on a social platform — client decision |
| Halliday, Real Review, Wine Front | — | — | paid, needs client sign-off |

Winepilot is a WordPress site, so its whole archive is one paginated endpoint:

```
GET winepilot.com/wp-json/wp/v2/posts?per_page=100&page=N
```
`points` carries the 100-point score (same scale as Halliday). 16,003 reviews in
~160 requests, no browser. 15,666 of them are scored.

**Review sites cover far more than retailers stock.** Winepilot linked 984 of
16,003; Crafty Pint 18 of 3,702 — its beers are independent craft releases
("Molly Rose Bohemian Hipsody") that mainstream bottle shops do not carry. The
reviews are kept in `review_queue.jsonl` and link automatically as retailer
coverage grows; they are not discarded.

Crafty Pint is Cloudflare-blocked server-side, but same-origin `fetch` from a
craftypint.com tab returns fully server-rendered HTML. The content script fetches
and parses (a service worker has no DOMParser) and the background worker relays to
the collector, because the page itself cannot reach localhost.

Only a minority link to catalogue products — Winepilot reviews far more wine than
any one retailer stocks — and unlinked reviews go to `review_queue.jsonl` rather
than being force-matched.

**Vintage is part of wine identity.** A one-digit vintage difference barely moves a
similarity score, so a 2022 tasting note happily attached to the 2000 of the same
wine. `resolve.py` rejects a match when both sides state a vintage and disagree;
that removed 883 false links.

## Pack size and price comparison

Pack size is the difference between a real comparison and a nonsense one, and
each retailer expresses it differently:

| Retailer | Where pack size comes from | Trap |
|---|---|---|
| Dan Murphy's | `.product-card-unit` = "pack (6)" / "case (24)" | its API reports `Unit: "Each"` even for a case |
| Thirsty Camel | the product **name** ("... 330ml 6pk") | `packQty` is the CARTON quantity, not the sellable pack — using it priced St Hallett Shiraz at $1.25/bottle |
| Liquorland | `.product-card-unit` | often absent, defaults to 1 |
| Nicks / Kent St | parsed from the name | mostly singles |

`resolve.py` compares in this order:

1. **`same_pack`** — same pack size, different retailers, cheapest price per
   retailer. The default and the most defensible.
2. **`unit_price`** — pack sizes differ but all are known, so per-unit is sound
   (one can at $8.00 vs a 4-pack at $22.99 is $8.00 vs $5.75 each).
3. Anything with a spread above 150% is flagged `compare_suspect` and must not be
   shown as a saving — real competition does not produce a 2.5x gap on an
   identical product, so it almost always means an unresolved pack mismatch.

Each entry carries `compare_basis`, `compare_pack_size`, `compare_prices`,
`compare_spread_pct` and `compare_cheapest`.

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
