const $ = (id) => document.getElementById(id);

async function refresh() {
  const { stats, last_error, adc_sweep, sweep_progress, adc_listing, listing_progress } =
    await chrome.storage.local.get(["stats", "last_error", "adc_sweep", "sweep_progress",
                                    "adc_listing", "listing_progress"]);
  const s = stats || { products: 0, reviews: 0, pages: 0 };
  $("p").textContent = s.products; $("r").textContent = s.reviews; $("g").textContent = s.pages;
  $("err").textContent = last_error ? String(last_error).slice(0, 70) : "";

  const { enrich_progress: ep, adc_enrich } =
    await chrome.storage.local.get(["enrich_progress", "adc_enrich"]);
  if (ep) {
    const running = adc_enrich && adc_enrich.active;
    $("en").textContent = `${ep.done}/${ep.total}` + (running ? "" : " (idle)");
  } else {
    $("en").textContent = "—";
  }

  const lp = listing_progress;
  if (adc_listing && adc_listing.active && lp) {
    $("ls").textContent = `${lp.done}/${lp.total}`;
  } else if (adc_listing && adc_listing.finished) {
    $("ls").textContent = "done";
  } else {
    $("ls").textContent = lp ? `${lp.done}/${lp.total} (idle)` : "—";
  }

  const q = sweep_progress;
  if (adc_sweep && adc_sweep.active && q) {
    $("sw").textContent = `${q.done}/${q.total}`;
  } else if (adc_sweep && adc_sweep.finished) {
    $("sw").textContent = "done";
  } else {
    $("sw").textContent = q ? `${q.done}/${q.total} (idle)` : "—";
  }
}

async function activeTab() {
  const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
  return t;
}

$("scrape").onclick = async () => {
  const t = await activeTab();
  chrome.tabs.sendMessage(t.id, { type: "ADC_SCRAPE_NOW" }, () => setTimeout(refresh, 1500));
};

$("crawl").onclick = async () => {
  const pages = Math.max(1, Number($("pages").value) || 20);
  await chrome.storage.local.set({ adc_crawl: { active: true, pages_left: pages } });
  const t = await activeTab();
  chrome.tabs.sendMessage(t.id, { type: "ADC_SCRAPE_NOW" }, () => setTimeout(refresh, 1500));
};

$("stop").onclick = async () => {
  await chrome.storage.local.set({ adc_crawl: { active: false, pages_left: 0 } });
};

$("enrich").onclick = () => {
  chrome.runtime.sendMessage({ type: "ADC_ENRICH_START" });
  setTimeout(refresh, 1500);
};
$("enrichstop").onclick = () => chrome.runtime.sendMessage({ type: "ADC_ENRICH_STOP" });

$("listing").onclick = async () => {
  const t = await activeTab();
  chrome.runtime.sendMessage({ type: "ADC_LISTING_START", tab_id: t.id,
    tabs: Math.max(1, Math.min(4, Number($("ltabs").value) || 1)) });
  setTimeout(refresh, 1500);
};

$("listingstop").onclick = () => chrome.runtime.sendMessage({ type: "ADC_LISTING_STOP" });

$("sweep").onclick = async () => {
  const t = await activeTab();
  chrome.runtime.sendMessage({
    type: "ADC_SWEEP_START", tab_id: t.id,
    max_pages: Math.max(1, Number($("revpages").value) || 5),
    tabs: Math.max(1, Math.min(4, Number($("tabs").value) || 1)),
  });
  setTimeout(refresh, 1500);
};

$("sweepstop").onclick = () => chrome.runtime.sendMessage({ type: "ADC_SWEEP_STOP" });

$("reset").onclick = async () => {
  await chrome.storage.local.set({ stats: { products: 0, reviews: 0, pages: 0 } });
  refresh();
};

refresh();
setInterval(refresh, 2000);
