// Buffers batches from content scripts and forwards them to the local collector.
const COLLECTOR = "http://127.0.0.1:8765/ingest";
const queue = [];
let flushing = false;
// Worker tabs that finished a product and are waiting for their next target.
const pendingTabs = new Set();
const pendingListingTabs = new Set();

async function flush() {
  if (flushing || !queue.length) return;
  flushing = true;
  try {
    while (queue.length) {
      const batch = queue.shift();
      try {
        const res = await fetch(COLLECTOR, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(batch),
        });
        if (!res.ok) throw new Error("HTTP " + res.status);
        const { stats } = await chrome.storage.local.get("stats");
        const s = stats || { products: 0, reviews: 0, pages: 0 };
        s.products += batch.products.length;
        s.reviews += batch.reviews.length;
        s.pages += 1;
        await chrome.storage.local.set({ stats: s, last_error: null, last_activity: Date.now() });
      } catch (e) {
        // Collector down: put it back and surface the problem in the popup.
        queue.unshift(batch);
        await chrome.storage.local.set({ last_error: String(e) });
        break;
      }
    }
  } finally { flushing = false; }
}

// ------------------------------------------------------------------ sweep loop
// Pulls product URLs from the collector one at a time and drives the tab through
// them. State lives in chrome.storage so it survives the service worker sleeping.
// Each worker tab gets its own target. The collector's in-flight lock is what
// makes this safe: two tabs can never be handed the same product.
async function sweepNext(tabId) {
  const { adc_sweep } = await chrome.storage.local.get("adc_sweep");
  if (!adc_sweep || !adc_sweep.active) return;
  const tab = tabId || adc_sweep.tab_id;

  let data;
  try {
    const res = await fetch(COLLECTOR.replace("/ingest", "/next"));
    data = await res.json();
  } catch (e) {
    await chrome.storage.local.set({ last_error: "collector unreachable: " + e });
    return;
  }

  if (!data.target) {
    // Queue empty: retire this worker. The run ends when the last one retires.
    const jobs = { ...(adc_sweep.jobs || {}) };
    delete jobs[tab];
    const remaining = Object.keys(jobs).length;
    await chrome.storage.local.set({
      adc_sweep: { ...adc_sweep, jobs, active: remaining > 0, finished: remaining === 0 },
      sweep_progress: data.progress,
    });
    return;
  }

  const jobs = { ...(adc_sweep.jobs || {}) };
  jobs[tab] = { url: data.target.url, name: data.target.name,
                max_reviews: data.target.max_reviews || null };
  await chrome.storage.local.set({
    adc_sweep: { ...adc_sweep, jobs, url: data.target.url, name: data.target.name,
                 max_reviews: data.target.max_reviews || null },
    sweep_progress: data.progress,
    last_activity: Date.now(),
  });
  try {
    await chrome.tabs.update(Number(tab), { url: data.target.url });
  } catch (e) {
    const j = { ...(adc_sweep.jobs || {}) };
    delete j[tab];
    await chrome.storage.local.set({
      adc_sweep: { ...adc_sweep, jobs: j, active: Object.keys(j).length > 0 },
      last_error: "sweep tab " + tab + " closed",
    });
  }
}

async function listingNext(tabId) {
  const { adc_listing } = await chrome.storage.local.get("adc_listing");
  if (!adc_listing || !adc_listing.active) return;
  const tab = tabId || adc_listing.tab_id;

  let data;
  try {
    const res = await fetch(COLLECTOR.replace("/ingest", "/next_listing"));
    data = await res.json();
  } catch (e) {
    await chrome.storage.local.set({ last_error: "collector unreachable: " + e });
    return;
  }
  if (!data.target) {
    const jobs = { ...(adc_listing.jobs || {}) };
    delete jobs[tab];
    const remaining = Object.keys(jobs).length;
    await chrome.storage.local.set({
      adc_listing: { ...adc_listing, jobs, active: remaining > 0, finished: remaining === 0 },
      listing_progress: data.progress,
    });
    return;
  }
  const jobs = { ...(adc_listing.jobs || {}) };
  jobs[tab] = { url: data.target.url, clicks: data.target.clicks };
  await chrome.storage.local.set({
    adc_listing: { ...adc_listing, jobs, url: data.target.url, clicks: data.target.clicks },
    listing_progress: data.progress,
    last_activity: Date.now(),
  });
  try {
    await chrome.tabs.update(Number(tab), { url: data.target.url });
  } catch (e) {
    const j = { ...(adc_listing.jobs || {}) };
    delete j[tab];
    await chrome.storage.local.set({
      adc_listing: { ...adc_listing, jobs: j, active: Object.keys(j).length > 0 },
      last_error: "listing tab " + tab + " closed",
    });
  }
}

// ------------------------------------------------------- API enrichment
// Dan Murphy's listing pages only render prices for tiles near the viewport, so
// a deep crawl leaves most products priceless. Their own product API returns
// price, pack size and rating for a batch of stockcodes, and runs from the
// extension (no page, no rendering, no throttling).
const DM_API = "https://api.danmurphys.com.au/apis/ui/Products/";

// Background-side diagnostics, to the same log the content scripts write to.
function blog(msg) {
  console.log("[ADC-bg] " + msg);
  fetch(COLLECTOR.replace("/ingest", "/log"), {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ where: "background", msg }),
  }).catch(() => {});
}

async function enrichNext() {
  const { adc_enrich } = await chrome.storage.local.get("adc_enrich");
  blog("enrichNext active=" + !!(adc_enrich && adc_enrich.active));
  if (!adc_enrich || !adc_enrich.active) return;

  let data;
  try {
    const res = await fetch(COLLECTOR.replace("/ingest", "/next_stockcodes"));
    data = await res.json();
  } catch (e) {
    await chrome.storage.local.set({ last_error: "collector unreachable: " + e });
    return;
  }
  const codes = data.stockcodes || [];
  blog("got " + codes.length + " stockcodes");
  if (!codes.length) {
    await chrome.storage.local.set({
      adc_enrich: { ...adc_enrich, active: false, finished: true },
      enrich_progress: data.progress,
    });
    return;
  }

  let enriched = [];
  try {
    const r = await fetch(DM_API + codes.join(","), { credentials: "include" });
    const j = await r.json();
    for (const p of Object.values(j)) {
      if (!p || !p.Stockcode) continue;
      const sp = p.Prices && (p.Prices.singleprice || p.Prices.SinglePrice);
      const mp = p.Prices && (p.Prices.inanysixprice || p.Prices.memberprice);
      enriched.push({
        stockcode: String(p.Stockcode),
        price: sp && sp.Value != null ? sp.Value : null,
        member_price: mp && mp.Value != null ? mp.Value : null,
        unit: p.Unit || null,
        package_size: p.PackageSize || null,
        rating: p.OverallRating || null,
        reviews: p.NumberOfReviews || null,
        in_stock: typeof p.IsPurchasable === "boolean" ? p.IsPurchasable : null,
        url: p.UrlFriendlyName
          ? "https://www.danmurphys.com.au/product/DM_" + p.Stockcode + "/" + p.UrlFriendlyName
          : null,
      });
    }
    blog("API returned " + enriched.length + " products");
  } catch (e) {
    blog("enrich fetch FAILED: " + e);
  }

  queue.push({ host: "api.danmurphys.com.au", url: "api://enrich",
               products: [], reviews: [], enriched });
  await chrome.storage.local.set({ enrich_progress: data.progress, last_activity: Date.now() });
  await flush();
  // Pace the API the way a browsing session would.
  chrome.alarms.create("adc_next_enrich", { when: Date.now() + 1200 + Math.random() * 800 });
}

chrome.runtime.onMessage.addListener((msg, sender) => {
  const senderTab = sender && sender.tab ? sender.tab.id : null;
  if (msg.type === "ADC_LOG") {
    fetch(COLLECTOR.replace("/ingest", "/log"), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ where: msg.where || ("tab" + senderTab), msg: msg.msg }),
    }).catch(() => {});
  }
  if (msg.type === "ADC_BATCH") { queue.push(msg.payload); flush(); }
  if (msg.type === "ADC_WHOAMI") {
    // A worker tab asks what it was sent here to do.
    chrome.storage.local.get(["adc_sweep", "adc_listing"]).then(({ adc_sweep, adc_listing }) => {
      if (adc_listing && adc_listing.active) {
        const job = adc_listing.jobs ? adc_listing.jobs[senderTab] : null;
        chrome.tabs.sendMessage(senderTab, { type: "ADC_LISTING_JOB", job, active: true });
        return;
      }
      const job = adc_sweep && adc_sweep.jobs ? adc_sweep.jobs[senderTab] : null;
      chrome.tabs.sendMessage(senderTab, { type: "ADC_JOB", job,
        active: !!(adc_sweep && adc_sweep.active), max_pages: adc_sweep && adc_sweep.max_pages });
    });
  }
  if (msg.type === "ADC_SWEEP_START") {
    // A listing crawl left active from earlier will navigate the same tab and
    // fight the sweep for control, so shut it down first.
    (async () => {
      const want = Math.max(1, Math.min(4, msg.tabs || 1));
      const tabIds = [msg.tab_id];
      for (let i = 1; i < want; i++) {
        try {
          // Extra workers go in their own window: Chrome throttles timers in
          // occluded tabs, and tabs stacked in one window are hidden by definition.
          const w = await chrome.windows.create({ url: "about:blank", focused: false,
                                                  width: 900, height: 700,
                                                  left: 60 * i, top: 60 * i });
          tabIds.push(w.tabs[0].id);
        } catch (e) { console.log("[ADC] could not open worker window", e); }
      }
      await chrome.storage.local.set({
        adc_crawl: { active: false, pages_left: 0 },
        adc_listing: { active: false },
        adc_sweep: { active: true, tab_id: msg.tab_id, tab_ids: tabIds, jobs: {},
                     max_pages: msg.max_pages, finished: false },
        last_activity: Date.now(),
      });
      for (const t of tabIds) await sweepNext(t);
    })();
  }
  if (msg.type === "ADC_LISTING_START") {
    (async () => {
      const want = Math.max(1, Math.min(4, msg.tabs || 1));
      const tabIds = [msg.tab_id];
      for (let i = 1; i < want; i++) {
        try {
          // Tile rather than stack: overlapping windows are "occluded" and Chrome
          // throttles their timers to a standstill (a 6,771-product page stayed at
          // 24 tiles for 8 minutes). Narrow side-by-side windows stay visible.
          const w = await chrome.windows.create({ url: "about:blank", focused: false,
                                                  width: 480, height: 900,
                                                  left: 490 * i, top: 0 });
          tabIds.push(w.tabs[0].id);
        } catch (e) { console.log("[ADC] could not open listing worker window", e); }
      }
      await chrome.storage.local.set({
        adc_crawl: { active: false, pages_left: 0 },
        adc_sweep: { active: false },
        adc_listing: { active: true, tab_id: msg.tab_id, tab_ids: tabIds, jobs: {}, finished: false },
        last_activity: Date.now(),
      });
      for (const t of tabIds) await listingNext(t);
    })();
  }
  if (msg.type === "ADC_LISTING_PAGE_DONE") {
    if (senderTab) pendingListingTabs.add(senderTab);
    chrome.storage.local.set({ last_activity: Date.now() });
    chrome.alarms.create("adc_next_listing", { when: Date.now() + 3000 + Math.random() * 2000 });
  }
  if (msg.type === "ADC_ENRICH_START") {
    blog("ADC_ENRICH_START received");
    chrome.storage.local.set({
      adc_enrich: { active: true, finished: false }, last_activity: Date.now(),
    }).then(() => enrichNext());
  }
  if (msg.type === "ADC_ENRICH_STOP") {
    chrome.storage.local.get("adc_enrich").then(({ adc_enrich }) =>
      chrome.storage.local.set({ adc_enrich: { ...(adc_enrich || {}), active: false } }));
  }
  if (msg.type === "ADC_LISTING_STOP") {
    chrome.storage.local.get("adc_listing").then(({ adc_listing }) =>
      chrome.storage.local.set({ adc_listing: { ...(adc_listing || {}), active: false } }));
  }
  if (msg.type === "ADC_SWEEP_STOP") {
    chrome.storage.local.get("adc_sweep").then(({ adc_sweep }) =>
      chrome.storage.local.set({ adc_sweep: { ...(adc_sweep || {}), active: false } }));
  }
  if (msg.type === "ADC_SWEEP_PAGE_DONE") {
    if (senderTab) pendingTabs.add(senderTab);
    // A plain setTimeout here is lost when Chrome terminates the MV3 service
    // worker (~30s idle), which silently stalls the sweep until something else
    // wakes it — that cost 2h40m on the first overnight run. Alarms survive
    // worker termination, so schedule the next target as an alarm instead.
    chrome.storage.local.set({ last_activity: Date.now() });
    chrome.alarms.create("adc_next", { when: Date.now() + 3000 + Math.random() * 1500 });
  }
});

// Watchdog: if a sweep is active but nothing has happened for a few minutes,
// the run has stalled (a wedged page, a lost callback) — nudge it along.
chrome.alarms.create("adc_watchdog", { periodInMinutes: 1 });

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === "adc_next") {
    await flush();
    const tabs = [...pendingTabs];
    pendingTabs.clear();
    if (tabs.length) { for (const t of tabs) await sweepNext(t); }
    else await sweepNext();
    return;
  }
  if (alarm.name === "adc_next_enrich") { await enrichNext(); return; }
  if (alarm.name === "adc_next_listing") {
    await flush();
    const tabs = [...pendingListingTabs];
    pendingListingTabs.clear();
    if (tabs.length) { for (const t of tabs) await listingNext(t); }
    else await listingNext();
    return;
  }
  if (alarm.name !== "adc_watchdog") return;

  const { adc_sweep, adc_listing, last_activity } =
    await chrome.storage.local.get(["adc_sweep", "adc_listing", "last_activity"]);
  const sweeping = adc_sweep && adc_sweep.active;
  const listing = adc_listing && adc_listing.active;
  if (!sweeping && !listing) return;
  const idleMs = Date.now() - (last_activity || 0);
  // Listing pages click "Show 24 more" up to 60 times, so allow much longer.
  if (idleMs < (listing ? 900000 : 240000)) return;
  console.log("[ADC] watchdog: idle " + Math.round(idleMs / 1000) + "s, resuming");
  await chrome.storage.local.set({ last_activity: Date.now() });
  await flush();
  if (listing) await listingNext(); else await sweepNext();
});

setInterval(flush, 5000);
