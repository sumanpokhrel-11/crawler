// Runs on every matched page: extracts, ships to the background worker, and can
// walk pagination on its own so an operator only has to start it once.
(() => {
  const SETTLE_MS = 1200;
  let running = false;

  function harvest() {
    const products = globalThis.AD_RETAILERS.run();
    const reviews = globalThis.AD_REVIEWS.run();
    return { url: location.href, host: location.hostname, products, reviews, captured_at: new Date().toISOString() };
  }

  // Dan Murphy's review pagination REPLACES the four visible cards rather than
  // appending, so every page must be harvested before advancing.
  function nextReviewButton() {
    for (const b of document.querySelectorAll("button")) {
      const label = b.getAttribute("aria-label") || "";
      if (/next page/i.test(label) && !b.disabled && b.offsetParent !== null) return b;
    }
    return null;
  }

  function firstReviewTitle() {
    const el = document.querySelector(".review-card__title, .review-card__ldesc");
    return el ? el.textContent.trim() : null;
  }

  // Long reviews render truncated with a "Read more" toggle, and the toggle's own
  // label ends up inside the text node. Expand every card before reading.
  async function expandReadMore() {
    let clicked = 0;
    for (const card of document.querySelectorAll(".review-card")) {
      for (const el of card.querySelectorAll("a, button, span")) {
        const t = (el.textContent || "").trim();
        if (/^read more$/i.test(t) && el.offsetParent !== null) {
          el.click();
          clicked++;
          break;
        }
      }
    }
    if (clicked) await new Promise((r) => setTimeout(r, 600));
    return clicked;
  }

  // Deep products run to thousands of pages, so reviews are flushed to the
  // collector in chunks. A crash then costs the current chunk, not the product.
  const REVIEW_CHUNK_PAGES = 25;

  async function harvestAllReviewPages(maxPages, onChunk, maxReviews) {
    const seen = new Set();
    let buffer = [];
    let pages = 0;
    let total = 0;

    for (let page = 0; page < maxPages; page++) {
      await expandReadMore();
      for (const r of globalThis.AD_REVIEWS.run()) {
        const k = (r.text || "").slice(0, 120) + "|" + (r.author || "");
        if (!seen.has(k)) { seen.add(k); buffer.push(r); }
      }
      pages++;
      // Stop as soon as the product's cap is met, rather than paging to the end.
      if (maxReviews && total + buffer.length >= maxReviews) {
        if (buffer.length > maxReviews - total) buffer.length = Math.max(0, maxReviews - total);
        break;
      }
      if (onChunk && pages % REVIEW_CHUNK_PAGES === 0 && buffer.length) {
        total += buffer.length;
        await onChunk(buffer, false);
        buffer = [];
      }

      const btn = nextReviewButton();
      if (!btn) break;
      const before = firstReviewTitle();
      btn.scrollIntoView({ block: "center" });
      btn.click();
      let advanced = false;
      for (let i = 0; i < 25; i++) {
        await new Promise((r) => setTimeout(r, 400));
        if (firstReviewTitle() !== before) { advanced = true; break; }
      }
      if (!advanced) break;                       // last page, or it stopped responding
      await new Promise((r) => setTimeout(r, 600 + Math.random() * 600));
    }
    total += buffer.length;
    return { tail: buffer, total, pages };
  }

  // Dan Murphy's defaults the review list to "Highest rating", so the first pages
  // are overwhelmingly positive: a product averaging 1.6 stars showed 5,4,4,4 on
  // page one. Sorting by Newest gives a chronological, unbiased sample.
  async function setReviewSort(preferred) {
    const wrap = document.querySelector(".customer-reviews__sort");
    const sel = wrap && wrap.querySelector("mat-select");
    if (!sel) return null;
    const current = (sel.textContent || "").trim();
    const want = new RegExp("^" + preferred + "$", "i");
    if (want.test(current)) return current;

    const before = firstReviewTitle();
    sel.scrollIntoView({ block: "center" });
    sel.click();
    await new Promise((r) => setTimeout(r, 1000));
    const panel = document.querySelector('.mat-select-panel, div[id^="mat-select"][role="listbox"]');
    const opt = [...(panel ? panel.querySelectorAll('mat-option, [role="option"]') : [])]
      .find((o) => want.test((o.textContent || "").trim()));
    if (!opt) {
      document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      return null;
    }
    opt.click();
    for (let i = 0; i < 25; i++) {
      await new Promise((r) => setTimeout(r, 400));
      if (firstReviewTitle() !== before) break;
    }
    await new Promise((r) => setTimeout(r, 700));
    return preferred;
  }

  // Seeded listing crawl: expand the infinite loader fully, then harvest once.
  // Angular renders the product grid well after document_idle. Starting the
  // expansion before the grid exists means the load-more button is not in the DOM
  // yet, so the crawl gave up and harvested a single screen (24 of 6771).
  async function waitForProducts(timeoutMs = 20000) {
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      if (document.querySelector(".product, .ProductTileV3")) return true;
      await new Promise((r) => setTimeout(r, 500));
    }
    return false;
  }

  async function runListingPage(job) {
    const ready = await waitForProducts();
    if (!ready) console.log("[ADC] listing: no product grid appeared");
    // clicks === 0 means the site paginates by URL (Liquorland ?page=N) and each
    // page is its own queue target, so there is no button to expand. Using
    // `job.clicks || 60` here would wrongly hunt for a load-more button.
    const budget = Number.isFinite(job.clicks) ? job.clicks : 60;
    const clicks = budget > 0 ? await expandInfiniteList(budget) : 0;
    await autoScroll();
    await new Promise((r) => setTimeout(r, SETTLE_MS));
    const products = globalThis.AD_RETAILERS.run();
    chrome.runtime.sendMessage({
      type: "ADC_BATCH",
      payload: { url: location.href, host: location.hostname, products, reviews: [],
                 listing_url: job.url, captured_at: new Date().toISOString() },
    });
    console.log(`[ADC] listing ${location.pathname}: ${clicks} clicks, ${products.length} products`);
    chrome.runtime.sendMessage({ type: "ADC_LISTING_PAGE_DONE", products: products.length });
  }

  async function runSweepPage(sweep) {
    // Wait for whichever review representation this site uses.
    //
    // Liquorland injects two JSON-LD nodes at different times: the Product node
    // (carrying aggregateRating) lands first, the `review` array later. Treating
    // aggregateRating as "reviews are here" fired the moment the Product node
    // appeared and harvested before any review text existed — 672 products that
    // advertised reviews returned nothing. Wait for the review array itself, and
    // only bail early when the Product node says there are no reviews at all.
    const ldText = () => [...document.querySelectorAll('script[type="application/ld+json"]')]
      .map((s) => s.textContent || "").join("");
    const hasReviewArray = () => /"review"\s*:\s*\[/.test(ldText());
    const hasCards = () => !!document.querySelector(".review-card");
    const productRendered = () => /"@type"\s*:\s*"Product"/.test(ldText());
    const claimsReviews = () => /"aggregateRating"/.test(ldText());

    for (let i = 0; i < 30; i++) {          // up to ~15s
      if (hasCards() || hasReviewArray()) break;
      if (productRendered() && !claimsReviews()) break;   // genuinely no reviews
      await new Promise((r) => setTimeout(r, 500));
    }

    const sort = await setReviewSort(sweep.sort || "Newest");
    const summary = globalThis.AD_REVIEWS.reviewSummary
      ? globalThis.AD_REVIEWS.reviewSummary() : null;

    const base = { url: location.href, host: location.hostname };
    let sentSummary = false;

    const flushChunk = async (reviews, final) => {
      for (const r of reviews) r.sort_order = sort;
      chrome.runtime.sendMessage({
        type: "ADC_BATCH",
        payload: {
          ...base,
          products: final ? globalThis.AD_RETAILERS.run() : [],
          reviews,
          review_summary: sentSummary ? null : summary,
          // Only the final chunk marks the product done, so an interrupted
          // product is retried rather than being recorded as complete.
          sweep_url: final ? sweep.url : null,
          captured_at: new Date().toISOString(),
        },
      });
      sentSummary = true;
      await new Promise((r) => setTimeout(r, 300));
    };

    const { tail, total } = await harvestAllReviewPages(
      sweep.max_pages || 2000, flushChunk, sweep.max_reviews || null);
    await flushChunk(tail, true);
    chrome.runtime.sendMessage({ type: "ADC_SWEEP_PAGE_DONE", reviews: total });
  }

  function send(payload) {
    if (!payload.products.length && !payload.reviews.length) return;
    chrome.runtime.sendMessage({ type: "ADC_BATCH", payload });
  }

  function nextPageHref() {
    const sels = ['a[rel="next"]', 'link[rel="next"]', 'a[aria-label*="Next" i]',
                  'a[class*="next"]:not([aria-disabled="true"])', '[data-testid*="next"] a'];
    for (const s of sels) {
      const el = document.querySelector(s);
      const href = el && (el.getAttribute("href") || el.href);
      if (href && !/^#/.test(href)) { try { return new URL(href, location.href).href; } catch {} }
    }
    return null;
  }

  // Many AU retailers (Dan Murphy's, BWS) use an infinite loader: a "Show 24 more"
  // button appends tiles to the same page instead of navigating. Clicking it N times
  // and harvesting once is both simpler and far gentler than N page loads.
  function loadMoreButton() {
    const sels = ["button.infinite-loader__load-more-button", 'button[class*="load-more"]',
                  'button[class*="loadMore"]', 'a[class*="load-more"]',
                  'button[class*="review"][class*="more"]'];
    for (const s of sels) {
      const el = document.querySelector(s);
      if (el && !el.disabled && el.offsetParent !== null) return el;
    }
    for (const el of document.querySelectorAll("button")) {
      if (/show\s*\d*\s*more|load\s*more/i.test(el.textContent || "") &&
          !el.disabled && el.offsetParent !== null) return el;
    }
    return null;
  }

  async function expandInfiniteList(maxClicks) {
    let clicks = 0;
    while (clicks < maxClicks) {
      // The load-more button sits below the fold and is not rendered until the
      // grid is scrolled into view. Searching from the top of the page finds
      // nothing and ends the crawl after a single screen of products.
      window.scrollTo(0, document.body.scrollHeight);
      await new Promise((r) => setTimeout(r, 500));
      let btn = loadMoreButton();
      if (!btn) {
        // One more try after letting lazy content settle.
        await new Promise((r) => setTimeout(r, 1200));
        window.scrollTo(0, document.body.scrollHeight);
        await new Promise((r) => setTimeout(r, 600));
        btn = loadMoreButton();
      }
      if (!btn) break;
      const countSel = document.querySelector(".review-card") ? ".review-card" : ".product";
      const before = document.querySelectorAll(countSel).length;
      btn.click();
      clicks++;
      // Wait for the new batch to render, then confirm the count actually grew.
      let grew = false;
      for (let i = 0; i < 20; i++) {
        await new Promise((r) => setTimeout(r, 400));
        if (document.querySelectorAll(countSel).length > before) { grew = true; break; }
      }
      if (!grew) break;                       // exhausted, or the list stopped responding
      await new Promise((r) => setTimeout(r, 800 + Math.random() * 700));  // politeness
    }
    return clicks;
  }

  async function autoScroll() {
    // Lazy-loaded grids only render tiles that have been near the viewport.
    let last = -1;
    for (let i = 0; i < 25 && document.body.scrollHeight !== last; i++) {
      last = document.body.scrollHeight;
      scrollTo(0, document.body.scrollHeight);
      await new Promise((r) => setTimeout(r, 600));
    }
    scrollTo(0, 0);
  }

  async function runOnce(opts = {}) {
    if (running) return;
    running = true;
    try {
      const { adc_crawl } = await chrome.storage.local.get("adc_crawl");
      const crawling = adc_crawl && adc_crawl.active;

      // Prefer expanding an infinite list in place over navigating page to page.
      if (crawling) {
        const clicks = await expandInfiniteList(adc_crawl.pages_left ?? 1);
        if (clicks > 0) {
          await autoScroll();
          await new Promise((r) => setTimeout(r, SETTLE_MS));
          send(harvest());
          await chrome.storage.local.set({ adc_crawl: { ...adc_crawl, active: false } });
          return;
        }
      }

      if (opts.scroll !== false) await autoScroll();
      await new Promise((r) => setTimeout(r, SETTLE_MS));
      send(harvest());

      if (crawling) {
        const next = nextPageHref();
        const budget = (adc_crawl.pages_left ?? 0) - 1;
        if (next && budget > 0) {
          await chrome.storage.local.set({ adc_crawl: { ...adc_crawl, pages_left: budget } });
          setTimeout(() => { location.href = next; }, 2000 + Math.random() * 1500);
        } else {
          await chrome.storage.local.set({ adc_crawl: { ...adc_crawl, active: false } });
        }
      }
    } finally { running = false; }
  }

  chrome.runtime.onMessage.addListener((msg, _s, reply) => {
    if (msg.type === "ADC_SCRAPE_NOW") { runOnce(msg.opts || {}).then(() => reply({ ok: true })); return true; }
  });

  // Auto-continue whichever job navigated itself here.
  // Dan Murphy's redirects product URLs to a canonical form (".../baileys-irish-
  // cream-700ml" can land on ".../baileys-irish-cream-700"), so an exact URL match
  // never fires. The DM_xxxxx product id survives the redirect; fall back to
  // "any product page" so a canonicalisation we have not seen cannot stall the sweep.
  function productId(u) {
    const m = String(u || "").match(/\/product\/([A-Za-z0-9_]+)/);
    return m ? m[1] : null;
  }

  function onExpectedPage(sweep) {
    if (!sweep || !sweep.url) return false;
    const want = productId(sweep.url);
    const here = productId(location.href);
    if (want && here) return want === here;
    return location.href.split("?")[0] === sweep.url.split("?")[0];
  }

  // With several worker tabs running, a tab cannot tell which product it was
  // sent to fetch by reading shared storage — every tab would read the newest
  // assignment. Ask the background worker instead: it knows the sender's tab id.
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type !== "ADC_JOB") return;
    if (!msg.active || !msg.job) return;
    const job = { ...msg.job, max_pages: msg.max_pages };
    if (onExpectedPage(job) || /\/product\//.test(location.href)) {
      console.log("[ADC] sweep page", productId(location.href));
      runSweepPage(job).catch((e) => {
        console.log("[ADC] sweep page failed:", e);
        chrome.runtime.sendMessage({ type: "ADC_SWEEP_PAGE_DONE", reviews: 0 });
      });
    } else {
      console.log("[ADC] sweep landed off-product, skipping:", location.pathname);
      chrome.runtime.sendMessage({ type: "ADC_SWEEP_PAGE_DONE", reviews: 0 });
    }
  });

  chrome.storage.local.get(["adc_crawl", "adc_sweep", "adc_listing"]).then(
    ({ adc_crawl, adc_sweep, adc_listing }) => {
    if (adc_listing && adc_listing.active && adc_listing.url &&
        location.href.split("?")[0] === adc_listing.url.split("?")[0]) {
      runListingPage(adc_listing).catch((e) => {
        console.log("[ADC] listing page failed:", e);
        chrome.runtime.sendMessage({ type: "ADC_LISTING_PAGE_DONE", products: 0 });
      });
      return;
    }
    if (adc_listing && adc_listing.active) {
      chrome.runtime.sendMessage({ type: "ADC_LISTING_PAGE_DONE", products: 0 });
      return;
    }
    if (adc_sweep && adc_sweep.active) {
      // Background replies with this tab's own job via ADC_JOB above.
      chrome.runtime.sendMessage({ type: "ADC_WHOAMI" });
      return;
    }
    if (adc_crawl && adc_crawl.active) runOnce();
  });
})();
