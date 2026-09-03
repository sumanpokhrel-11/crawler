"""Canonicalisation helpers shared by the HTTP lane and the extension lane.

Everything that turns messy retailer text into the fields in schema/records.schema.json
lives here, so both lanes produce byte-identical records for the same product.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

# --- volume -----------------------------------------------------------------
# Retailers write the same thing a dozen ways: "750ml", "750 mL", "0.75L", "1.125 Litre".
_VOL_ML = re.compile(r"(\d+(?:\.\d+)?)\s*(?:ml|mls|millilitre?s?)\b", re.I)
_VOL_L = re.compile(r"(\d+(?:\.\d+)?)\s*(?:l|lt|ltr|litre?s?|liter?s?)\b", re.I)

def parse_volume_ml(text: str | None) -> int | None:
    if not text:
        return None
    m = _VOL_ML.search(text)
    if m:
        return int(round(float(m.group(1))))
    m = _VOL_L.search(text)
    if m:
        return int(round(float(m.group(1)) * 1000))
    return None

# --- pack size --------------------------------------------------------------
# "Case of 12", "6 Pack", "24 x 375ml", "6pk"
_PACK = [
    re.compile(r"\bcase\s+of\s+(\d+)\b", re.I),
    re.compile(r"\b(\d+)\s*(?:x|×)\s*\d+\s*ml\b", re.I),
    re.compile(r"\b(\d+)\s*[- ]?(?:pack|pk)\b", re.I),
    # Dan Murphy's prints the unit as "pack (6)" rather than "6 pack".
    re.compile(r"\b(?:pack|case|pk)\s*\(\s*(\d+)\s*\)", re.I),
]

def parse_pack_size(text: str | None) -> int | None:
    if not text:
        return None
    for rx in _PACK:
        m = rx.search(text)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 48:
                return n
    return None

# --- abv --------------------------------------------------------------------
_ABV = re.compile(r"(\d{1,2}(?:\.\d)?)\s*%\s*(?:abv|alc|alcohol)?", re.I)

def parse_abv(text: str | None) -> float | None:
    if not text:
        return None
    m = _ABV.search(text)
    if not m:
        return None
    v = float(m.group(1))
    return v if 0 < v <= 80 else None

# --- vintage ----------------------------------------------------------------
# Only treat a bare 4-digit number as a vintage if it is a plausible wine year.
_YEAR = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")

def parse_vintage(text: str | None) -> int | None:
    if not text:
        return None
    now = datetime.now(timezone.utc).year
    years = [int(y) for y in _YEAR.findall(text)]
    years = [y for y in years if y <= now]
    return max(years) if years else None

# --- category ---------------------------------------------------------------
_CATEGORY_HINTS = [
    ("rtd", ["premix", "ready to drink", "rtd", "seltzer", "canned cocktail"]),
    ("cider", ["cider", "perry"]),
    ("sake", ["sake", "shochu"]),
    ("beer", ["beer", "ale", "lager", "ipa", "stout", "pilsner", "porter", "sour", "gose", "saison"]),
    ("spirits", ["whisky", "whiskey", "bourbon", "scotch", "gin", "vodka", "rum", "tequila",
                  "brandy", "cognac", "liqueur", "mezcal", "absinthe", "single malt"]),
    ("wine", ["wine", "shiraz", "cabernet", "chardonnay", "pinot", "riesling", "merlot",
               "sauvignon", "grenache", "champagne", "prosecco", "sparkling", "rose", "rosé",
               "semillon", "tempranillo", "syrah", "moscato", "port", "sherry"]),
]

# Match hints on word boundaries only. Substring matching silently misfiles
# real products: "rum" is inside "Tumbarumba", "port" inside "Portugal".
_CATEGORY_RX = [
    (cat, re.compile(r"(?<![a-z])(?:" + "|".join(re.escape(h) for h in hints) + r")(?![a-z])", re.I))
    for cat, hints in _CATEGORY_HINTS
]

def guess_category(*texts: str | None) -> str | None:
    """Order matters: 'sparkling ale' is beer, so beer is tested before wine."""
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return None
    for cat, rx in _CATEGORY_RX:
        if rx.search(blob):
            return cat
    return "other"

# --- price ------------------------------------------------------------------
_PRICE = re.compile(r"(\d+(?:,\d{3})*(?:\.\d{1,2})?)")

def parse_price(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    m = _PRICE.search(str(value).replace("$", ""))
    if not m:
        return None
    try:
        return round(float(m.group(1).replace(",", "")), 2)
    except ValueError:
        return None

# --- name cleanup -----------------------------------------------------------
_NOISE = re.compile(
    r"\b(?:case of \d+|\d+\s*(?:x|×)\s*\d+\s*ml|\d+\s*[- ]?(?:pack|pk)|"
    r"\d+(?:\.\d+)?\s*(?:ml|l|lt|ltr|litre?s?)|nv|non[- ]vintage)\b",
    re.I,
)

# Dan Murphy's product-page <h1> runs brand into name with no space
# ("BaileysEspresso Creme Liqueur"), which blocks matching against listing data.
# Split only where a real word boundary is implied: not inside "McGuigan"/"MacLeod",
# and not on the "L" of a volume, so this must run *after* volume text is stripped.
_RUN_ON = re.compile(r"(?<=[a-z])(?=[A-Z][a-z])")
_NO_SPLIT_PREFIX = re.compile(r"\b(Ma?c)\s+([A-Z])")


def split_run_on(name: str | None) -> str:
    if not name:
        return ""
    return _NO_SPLIT_PREFIX.sub(r"\1\2", _RUN_ON.sub(" ", name))


def clean_name(name: str | None) -> str:
    if not name:
        return ""
    s = split_run_on(_NOISE.sub(" ", name))
    s = re.sub(r"[–—]", "-", s)
    # Stripping "case of 24" then "375ml" from "Case of 24 x 375ml" leaves a
    # dangling separator; drop orphaned x/× tokens left behind by that.
    s = re.sub(r"(?:^|\s)[x×](?=\s|$)", " ", s, flags=re.I)
    s = re.sub(r"\s*[-|,/]\s*$", "", s)
    return re.sub(r"\s{2,}", " ", s).strip()

def match_slug(brand, name, volume_ml, vintage) -> str:
    """Lowercased, punctuation-free identity string. Two retailers listing the same
    bottle should land on the same slug; this is what product_key hashes."""
    parts = [clean_name(brand), clean_name(name)]
    base = " ".join(p for p in parts if p).lower()
    base = re.sub(r"[^a-z0-9\s]", " ", base)
    base = re.sub(r"\s{2,}", " ", base).strip()
    # De-duplicate repeated brand tokens ("Penfolds Penfolds Bin 389")
    seen, toks = set(), []
    for t in base.split():
        if t not in seen:
            seen.add(t)
            toks.append(t)
    base = " ".join(toks)
    if volume_ml:
        base += f" {volume_ml}ml"
    if vintage:
        base += f" {vintage}"
    return base

def product_key(brand, name, volume_ml, vintage) -> str:
    slug = match_slug(brand, name, volume_ml, vintage)
    return hashlib.sha1(slug.encode("utf-8")).hexdigest()[:16]

def review_key(source: str, source_url: str, author, text: str) -> str:
    raw = f"{source}|{source_url}|{author or ''}|{(text or '')[:200]}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def norm_rating(raw, scale):
    if raw is None or not scale:
        return None
    try:
        return round(max(0.0, min(1.0, float(raw) / float(scale))), 4)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
