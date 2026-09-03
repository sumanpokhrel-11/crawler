// Retailer adapters -> raw product/offer payloads.
// Strategy for every site: embedded JSON first (JSON-LD / __NEXT_DATA__), DOM last.
globalThis.AD_RETAILERS = (() => {
  const { text, q, qa, pick, attr, jsonLd, abs } = globalThis.ADX;

  // Generic JSON-LD Product reader — covers most storefronts without custom code.
  function fromJsonLd(source, retailer) {
    const out = [];
    for (const n of jsonLd(["Product"])) {
      const offers = Array.isArray(n.offers) ? n.offers : (n.offers ? [n.offers] : []);
      const name = typeof n.name === "string" ? n.name : null;
      if (!name) continue;
      const brand = typeof n.brand === "string" ? n.brand : (n.brand && n.brand.name) || null;
      const img = Array.isArray(n.image) ? n.image[0] : n.image;
      const base = {
        source, retailer, source_url: abs(n.url) || location.href,
        source_product_id: n.sku || n.productID || null,
        name_raw: name, brand_raw: brand,
        description_raw: typeof n.description === "string" ? n.description : null,
        image_url: abs(typeof img === "string" ? img : (img && img.url) || null),
        gtin: n.gtin13 || n.gtin12 || n.gtin || null,
        subcategory_raw: typeof n.category === "string" ? n.category : null,
      };
      if (!offers.length) { out.push({ ...base, price_raw: null, availability_raw: null }); continue; }
      for (const o of offers) {
        out.push({
          ...base,
          price_raw: o.price != null ? String(o.price) : (o.lowPrice != null ? String(o.lowPrice) : null),
          was_price_raw: o.highPrice != null ? String(o.highPrice) : null,
          availability_raw: o.availability || null,
          currency: o.priceCurrency || "AUD",
        });
      }
    }
    return out;
  }

  // Endeavour Group sites (Dan Murphy's, BWS). Angular app: no data-testid
  // attributes and no JSON-LD Product data, so these are DOM selectors verified
  // against a live danmurphys.com.au listing page on 2026-09-03.
  //   .product            one product tile (24 per page load)
  //   .title / .subtitle  brand and product name (h2 concatenates them unspaced)
  //   .card-price         the displayed price
  //   .card-view-price    full block, contains the "Non-Member: $x" line
  // Dan Murphy's shows member and non-member prices; both are captured, since a
  // price comparison built on member-only pricing would be wrong for most shoppers.
  function endeavourTiles(source, retailer) {
    const money = (s) => { const m = (s || "").match(/\$\s*([\d,]+(?:\.\d{2})?)/); return m ? m[1] : null; };
    const out = [];
    for (const t of qa(document, ".product")) {
      const full = text(t) || "";

      // Dan Murphy's renders two different tile layouts on the same page.
      //   A: .title / .subtitle  + .card-price
      //   B: .product__title > .rich-text-renderer  + .product__price-value
      // Handling only A silently dropped roughly half the prices on category pages.
      let brand = text(q(t, ".title"));
      let sub = text(q(t, ".subtitle"));
      if (!brand) {
        const parts = qa(t, ".product__title .rich-text-renderer, .product__title .line-clamp")
          .map(text).filter(Boolean);
        if (parts.length) { brand = parts[0]; sub = parts.slice(1).join(" "); }
      }
      const name = [brand, sub].filter(Boolean).join(" ");
      if (!name) continue;

      const shown = money(text(q(t, ".card-price"))) || money(text(q(t, ".product__price-value")));
      const nonMember = (full.match(/Non-?Member:?\s*\$\s*([\d,]+(?:\.\d{2})?)/i) || [])[1] || null;
      const isMemberOffer = /member\s*offer/i.test(full);
      const revTxt = text(q(t, ".no-of-reviews"))
        || text(q(t, ".product__reviews-count--full"))
        || text(q(t, ".product__reviews-count--compact")) || "";

      out.push({
        source, retailer,
        source_url: abs(attr(t, ['a[href*="/product/"]'], "href")) || location.href,
        source_product_id: t.getAttribute("data-product-id") || null,
        name_raw: name,
        brand_raw: brand,
        // Non-member is what a general shopper pays, so it is the headline price.
        price_raw: nonMember || shown,
        member_price_raw: isMemberOffer ? shown : null,
        was_price_raw: null,
        promo_raw: (full.match(/(buy one get one free|\d+ for \$?\d+|save \$?[\d.]+)/i) || [])[1] || null,
        aggregate_rating_raw: qa(t, ".rating-icon.checked").length || null,
        aggregate_rating_scale: 5,
        aggregate_review_count: Number((revTxt.match(/(\d+)/) || [])[1]) || null,
        // Authoritative pack size: "pack (6)", "case (24)", "each".
        unit_raw: text(q(t, ".product-card-unit")) || text(q(t, ".product__price-quantity")),
        image_url: abs(attr(t, ["img"], "src") || attr(t, ["img"], "data-src")),
        availability_raw: pick(t, ['[class*="stock"]', '[class*="availability"]']),
        subcategory_raw: null, gtin: null, description_raw: null, currency: "AUD",
      });
    }
    return out;
  }

  // Liquorland (Coles group). Server-side blocked by ShieldSquare but fine in a
  // real browser. Verified live 2026-09-03: 60 .ProductTileV3 per page, paginated
  // by ?page=N. Prices are split across .dollarAmount/.centsAmount spans, and the
  // same markup is reused for current / was / saving, distinguished by the
  // .current and .saving classes on the enclosing .PriceTagV3.
  function liquorlandTiles(source, retailer) {
    const amount = (tag) => {
      if (!tag) return null;
      const d = q(tag, ".dollarAmount");
      if (!d) return null;
      const c = q(tag, ".centsAmount");
      return `${text(d)}.${c ? text(c) : "00"}`;
    };
    const out = [];
    for (const t of qa(document, ".ProductTileV3")) {
      const brand = text(q(t, ".product-brand"));
      const name = text(q(t, ".product-name"));
      if (!name) continue;
      const current = amount(q(t, ".PriceTagV3.current"));
      const was = amount(q(t, ".PriceTagV3.was, .PriceTagV3.secondary:not(.saving)"));
      out.push({
        source, retailer,
        source_url: abs(attr(t, ["a[href]"], "href")) || location.href,
        source_product_id: (attr(t, ["a[href]"], "href") || "").split("_").pop() || null,
        name_raw: [brand, name].filter(Boolean).join(" "),
        brand_raw: brand,
        price_raw: current,
        was_price_raw: was && current && parseFloat(was) > parseFloat(current) ? was : null,
        member_price_raw: null,
        promo_raw: null,
        // Listing tiles carry no ratings; reviews live on the product page.
        aggregate_rating_raw: null, aggregate_rating_scale: null, aggregate_review_count: null,
        unit_raw: text(q(t, ".unitOfMeasure")),
        image_url: abs(attr(t, ["img"], "src") || attr(t, ["img"], "data-src")),
        availability_raw: pick(t, ['[class*="stock"]', '[class*="unavailable"]']),
        subcategory_raw: null, gtin: null, description_raw: null, currency: "AUD",
      });
    }
    return out;
  }

  const SITES = [
    { match: /danmurphys\.com\.au$/,     source: "dan_murphys",   retailer: "Dan Murphy's",
      extract: (s, r) => [...fromJsonLd(s, r), ...endeavourTiles(s, r)] },
    { match: /bws\.com\.au$/,            source: "bws",           retailer: "BWS",
      extract: (s, r) => [...fromJsonLd(s, r), ...endeavourTiles(s, r)] },
    { match: /liquorland\.com\.au$/,     source: "liquorland",    retailer: "Liquorland",
      extract: (s, r) => { const t = liquorlandTiles(s, r); return t.length ? t : fromJsonLd(s, r); } },
    { match: /thirstycamel\.com\.au$/,   source: "thirsty_camel", retailer: "Thirsty Camel",
      extract: (s, r) => fromJsonLd(s, r) },
    { match: /nicks\.com\.au$/,          source: "nicks",         retailer: "Nicks Wine Merchants",
      extract: (s, r) => fromJsonLd(s, r) },
    { match: /mybottleshop\.com\.au$/,   source: "mybottleshop",  retailer: "MyBottleShop",
      extract: (s, r) => fromJsonLd(s, r) },
    { match: /liquorloot\.com\.au$/,     source: "liquor_loot",   retailer: "Liquor Loot",
      extract: (s, r) => fromJsonLd(s, r) },
  ];

  function run() {
    const host = location.hostname.replace(/^www\./, "");
    const site = SITES.find((s) => s.match.test(host));
    if (!site) return [];
    let items = [];
    try { items = site.extract(site.source, site.retailer) || []; }
    catch (e) { console.log("[ADC] retailer adapter error", e); }
    // De-duplicate: JSON-LD and DOM tiles often describe the same product.
    const seen = new Set();
    return items.filter((i) => {
      const k = `${i.name_raw}|${i.price_raw}|${i.source_url}`;
      if (seen.has(k)) return false;
      seen.add(k); return true;
    });
  }
  return { run };
})();
