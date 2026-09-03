// Shared extraction helpers. Adapters return RAW strings; all parsing and
// normalisation happens server-side in normalize.py so the HTTP lane and the
// extension lane emit byte-identical records.
globalThis.ADX = (() => {
  const text = (el) => (el && el.textContent ? el.textContent.replace(/\s+/g, " ").trim() : null);
  const q = (root, sel) => { try { return root.querySelector(sel); } catch { return null; } };
  const qa = (root, sel) => { try { return Array.from(root.querySelectorAll(sel)); } catch { return []; } };
  const pick = (root, sels) => { for (const s of sels) { const t = text(q(root, s)); if (t) return t; } return null; };
  const attr = (root, sels, name) => {
    for (const s of sels) { const e = q(root, s); const v = e && e.getAttribute(name); if (v) return v; }
    return null;
  };

  // Most modern storefronts ship the full product object in embedded JSON.
  // Reading that is far more stable than scraping generated class names.
  function jsonLd(types) {
    const out = [];
    for (const s of qa(document, 'script[type="application/ld+json"]')) {
      let data; try { data = JSON.parse(s.textContent); } catch { continue; }
      const stack = Array.isArray(data) ? [...data] : [data];
      while (stack.length) {
        const n = stack.pop();
        if (!n || typeof n !== "object") continue;
        if (Array.isArray(n["@graph"])) stack.push(...n["@graph"]);
        const t = n["@type"];
        const tl = Array.isArray(t) ? t : [t];
        if (tl.some((x) => types.includes(x))) out.push(n);
        for (const v of Object.values(n)) if (v && typeof v === "object") stack.push(v);
      }
    }
    return out;
  }

  function embeddedState() {
    const keys = ["__NEXT_DATA__", "__NUXT__", "__INITIAL_STATE__", "__APOLLO_STATE__", "digitalData"];
    const found = {};
    for (const k of keys) { try { if (globalThis[k]) found[k] = globalThis[k]; } catch {} }
    const nd = document.getElementById("__NEXT_DATA__");
    if (nd && !found.__NEXT_DATA__) { try { found.__NEXT_DATA__ = JSON.parse(nd.textContent); } catch {} }
    return found;
  }

  const abs = (u) => { try { return u ? new URL(u, location.href).href : null; } catch { return null; } };
  return { text, q, qa, pick, attr, jsonLd, embeddedState, abs };
})();
