// Review adapters. The client explicitly needs review TEXT, so every adapter
// must return a non-empty body; score-only rows are dropped.
globalThis.AD_REVIEWS = (() => {
  const { text, q, qa, pick, attr, jsonLd, abs } = globalThis.ADX;

  function fromJsonLd(source, subject_type) {
    const out = [];
    for (const n of jsonLd(["Review", "UserReview"])) {
      const body = n.reviewBody || n.description;
      if (!body || typeof body !== "string") continue;
      const rr = n.reviewRating || {};
      out.push({
        source, subject_type, source_url: location.href,
        product_name_raw: (n.itemReviewed && n.itemReviewed.name) || null,
        author: (n.author && (n.author.name || n.author)) || null,
        title: typeof n.name === "string" ? n.name : null,
        text: body,
        rating_raw: rr.ratingValue != null ? Number(rr.ratingValue) : null,
        rating_scale: rr.bestRating != null ? Number(rr.bestRating) : null,
        review_date: n.datePublished || null,
      });
    }
    return out;
  }

  function untappd() {
    const out = [];
    for (const el of qa(document, ".activity .item, #main-stream .item")) {
      const body = pick(el, [".comment-text", ".checkin-comment p"]);
      if (!body) continue;
      const capRaw = attr(el, ["[data-rating]"], "data-rating");
      out.push({
        source: "untappd", subject_type: "product", source_url: location.href,
        product_name_raw: pick(el, [".name .beer", ".beer-details .name a", ".text .name a"]),
        author: pick(el, [".text .user", ".name .user"]),
        title: null, text: body,
        rating_raw: capRaw ? Number(capRaw) : null,
        rating_scale: 5,
        review_date: attr(el, ["[data-gregtime]", "time"], "data-gregtime") || attr(el, ["time"], "datetime"),
      });
    }
    return out;
  }

  function trustpilot() {
    const out = [];
    for (const el of qa(document, 'article[data-service-review-card-paper], section[class*="reviewCard"]')) {
      const body = pick(el, ['[data-service-review-text-typography]', 'p[class*="typography_body"]']);
      if (!body) continue;
      out.push({
        source: "trustpilot_au", subject_type: "retailer", source_url: location.href,
        product_name_raw: null,
        author: pick(el, ['[data-consumer-name-typography]']),
        title: pick(el, ['[data-service-review-title-typography]', "h2"]),
        text: body,
        rating_raw: Number((attr(el, ['[data-service-review-rating] img'], "alt") || "").match(/\d/)?.[0]) || null,
        rating_scale: 5,
        review_date: attr(el, ["time"], "datetime"),
      });
    }
    return out;
  }

  function productReview() {
    const out = [];
    for (const el of qa(document, 'div[itemprop="review"], article[class*="review"]')) {
      const body = pick(el, ['[itemprop="reviewBody"]', 'p[class*="body"]']);
      if (!body) continue;
      out.push({
        source: "productreview_au", subject_type: "retailer", source_url: location.href,
        product_name_raw: null,
        author: pick(el, ['[itemprop="author"]', '[class*="author"]']),
        title: pick(el, ['[itemprop="name"]', "h3"]),
        text: body,
        rating_raw: Number(attr(el, ['[itemprop="ratingValue"]'], "content")) || null,
        rating_scale: 5,
        review_date: attr(el, ["time"], "datetime"),
      });
    }
    return out;
  }

  function craftyPint() {
    const out = [];
    const body = qa(document, ".article-body p, .entry-content p").map(text).filter(Boolean).join("\n\n");
    if (body && body.length > 200) {
      out.push({
        source: "crafty_pint", subject_type: "product", source_url: location.href,
        product_name_raw: pick(document, ["h1"]),
        author: pick(document, ['[rel="author"]', ".author-name"]),
        title: pick(document, ["h1"]), text: body,
        rating_raw: null, rating_scale: null,
        review_date: attr(document, ["time"], "datetime"),
      });
    }
    return out;
  }

  // Dan Murphy's / BWS product-page reviews. Verified against a live product page
  // 2026-09-03. Cards render inside <shop-customer-reviews>:
  //   .review-card__title  headline      .review-card__sdesc  author
  //   .review-card__ldesc  the body      .review-card__info   date ("19 June 2026")
  // Only a handful render initially; content.js expands the list before harvesting.
  function endeavourProductReviews(source) {
    const out = [];
    const productName = pick(document, ["h1"]);
    // Strip a trailing "Read more"/"Read less" toggle label that sits inside the
    // same text node as the review body.
    // Also drop the caret glyphs the expand/collapse control leaves in the text.
    const strip = (t) => (t || "")
      .replace(/\s*Read (?:more|less)\s*$/i, "")
      .replace(/[\s^]+$/, "")
      .trim();
    for (const c of qa(document, ".review-card")) {
      const body = strip(pick(c, [".review-card__ldesc"]));
      if (!body) continue;
      out.push({
        source, subject_type: "product", source_url: location.href,
        product_name_raw: productName,
        author: pick(c, [".review-card__sdesc"]),
        title: pick(c, [".review-card__title"]),
        text: body,
        rating_raw: qa(c, ".rating-icon.checked").length || null,
        rating_scale: 5,
        review_date: pick(c, [".review-card__info"]),
      });
    }
    return out;
  }

  // Liquorland renders reviews only into JSON-LD (the Bazaarvoice widget on the
  // page shows just a star summary, and its review list never renders inline).
  // The review objects sit in a `review` array on a node that carries no @type,
  // so the generic Review-type reader above cannot see them. They use
  // `headline`/`reviewBody`/`datePublished` with a plain-string author.
  function jsonLdReviewArrays(source) {
    const out = [];
    const productName = pick(document, ["h1"]);
    for (const sc of qa(document, 'script[type="application/ld+json"]')) {
      let data;
      try { data = JSON.parse(sc.textContent); } catch { continue; }
      const nodes = Array.isArray(data) ? data : [data];
      for (const n of nodes) {
        const arr = Array.isArray(n && n.review) ? n.review : (n && n.review ? [n.review] : []);
        for (const r of arr) {
          const body = r.reviewBody || r.description;
          if (!body || typeof body !== "string") continue;
          const rr = r.reviewRating || {};
          out.push({
            source, subject_type: "product", source_url: location.href,
            product_name_raw: productName,
            author: typeof r.author === "object" ? (r.author && r.author.name) : r.author,
            title: r.headline || r.name || null,
            text: body,
            rating_raw: rr.ratingValue != null ? Number(rr.ratingValue) : null,
            rating_scale: rr.bestRating != null ? Number(rr.bestRating) : 5,
            review_date: r.datePublished || r.dateCreated || null,
          });
        }
      }
    }
    return out;
  }

  // Aggregate rating from a JSON-LD Product node.
  function jsonLdSummary() {
    for (const n of jsonLd(["Product"])) {
      const ag = n.aggregateRating;
      if (!ag) continue;
      const count = Number(ag.reviewCount || ag.ratingCount) || null;
      return {
        product_name_raw: n.name || pick(document, ["h1"]),
        source_url: location.href,
        average_rating: ag.ratingValue != null ? Number(ag.ratingValue) : null,
        rating_scale: Number(ag.bestRating) || 5,
        pct_recommend: null,
        distribution: {},
        total_reviews: count,
      };
    }
    return null;
  }

  // The site's own rating snapshot is ground truth for the aggregate: our own
  // average over a sampled page or two is not representative of thousands of reviews.
  function endeavourReviewSummary() {
    const wrap = q(document, "shop-customer-reviews") || q(document, "shop-review-snapshot");
    if (!wrap) return null;
    const blob = text(wrap) || "";
    const dist = {};
    for (const row of qa(document, ".rating-chart__row")) {
      const star = text(q(row, ".rating-chart__rating"));
      const n = text(q(row, ".rating-chart__review-count"));
      if (star && n) dist[star] = Number(String(n).replace(/[^\d]/g, "")) || 0;
    }
    const avg = (blob.match(/average customer rating:?\s*([\d.]+)/i) || [])[1];
    const rec = (blob.match(/(\d+)%\s*of reviewers would recommend/i) || [])[1];
    if (!avg && !Object.keys(dist).length) return null;
    return {
      product_name_raw: pick(document, ["h1"]),
      source_url: location.href,
      average_rating: avg ? Number(avg) : null,
      rating_scale: 5,
      pct_recommend: rec ? Number(rec) : null,
      distribution: dist,
      total_reviews: Object.values(dist).reduce((a, b) => a + b, 0) || null,
    };
  }

  const SITES = [
    { match: /danmurphys\.com\.au$/,   fn: () => endeavourProductReviews("dan_murphys") },
    { match: /bws\.com\.au$/,          fn: () => endeavourProductReviews("bws") },
    { match: /liquorland\.com\.au$/,   fn: () => jsonLdReviewArrays("liquorland") },
    { match: /untappd\.com$/,          fn: () => [...untappd(), ...fromJsonLd("untappd", "product")] },
    { match: /trustpilot\.com$/,       fn: () => [...trustpilot(), ...fromJsonLd("trustpilot_au", "retailer")] },
    { match: /productreview\.com\.au$/,fn: () => [...productReview(), ...fromJsonLd("productreview_au", "retailer")] },
    { match: /craftypint\.com$/,       fn: () => [...craftyPint(), ...fromJsonLd("crafty_pint", "product")] },
  ];

  function run() {
    const host = location.hostname.replace(/^www\./, "");
    const site = SITES.find((s) => s.match.test(host));
    let items = [];
    if (site) { try { items = site.fn() || []; } catch (e) { console.log("[ADC] review adapter error", e); } }
    else { // Retailer pages often carry on-page product reviews too.
      items = fromJsonLd(host.replace(/\W+/g, "_"), "product");
    }
    const seen = new Set();
    return items.filter((i) => {
      if (!i.text || i.text.length < 15) return false;   // review text is the deliverable
      const k = i.text.slice(0, 120) + "|" + (i.author || "");
      if (seen.has(k)) return false;
      seen.add(k); return true;
    });
  }
  // Product-page summary: Dan Murphy's renders its own snapshot; everyone else
  // gets it from JSON-LD aggregateRating.
  function reviewSummary() {
    return endeavourReviewSummary() || jsonLdSummary();
  }

  return { run, endeavourReviewSummary, reviewSummary };
})();
