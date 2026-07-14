"""
Goal watch — monitor a target for a CONDITION, not just marketplace listings.

The north-star reframe (2026-07-13): a watch is "watch this and tell me when a condition is
true / collect this data." Marketplace listings is one template; RESTOCK ("tell me when size
34W x 30L is back in stock") is another. The principle that makes it reliable:

    USE THE BEST SIGNAL AVAILABLE.
    • A clean data endpoint when one exists — Shopify exposes /products/<handle>.js with an
      exact `available: true/false` per variant. Deterministic, free, no false alerts, no LLM.
    • LLM page-reading (Deep Inspect) as the general fallback for sites without such an endpoint.

This module is the first slice: the restock check. It dispatches to the Shopify data path when
it can, and reports a structured STATE the scheduler compares run-to-run to detect the flip
(out-of-stock → in-stock) and alert.

KEY LOCATIONS
  shopify_handle        ~L40   is this a Shopify product URL? → the product handle
  fetch_shopify_product ~L55   GET the product .js JSON (variants + availability)
  match_variant         ~L80   find the variant by id (?variant=) or by size text ("34W x 30L")
  check_restock         ~L120  the restock check → structured state {available, ...}
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)


def shopify_handle(url: str) -> Optional[str]:
    """The Shopify product handle if this looks like a Shopify product URL, else None.
    Shopify product pages are always /products/<handle> (optionally under /collections/...)."""
    try:
        m = re.search(r"/products/([a-z0-9][a-z0-9\-_]*)", urlparse(url).path, re.I)
        return m.group(1) if m else None
    except Exception:
        return None


def _variant_id_from_url(url: str) -> Optional[int]:
    try:
        q = dict(p.split("=", 1) for p in (urlparse(url).query or "").split("&") if "=" in p)
        v = q.get("variant")
        return int(v) if v and v.isdigit() else None
    except Exception:
        return None


def fetch_shopify_product(url: str, timeout: float = 30.0) -> Optional[dict]:
    """Fetch a Shopify product's data (title + variants with availability) from its public
    /products/<handle>.js endpoint. Returns the parsed dict, or None if it isn't Shopify or
    the endpoint doesn't respond as expected."""
    handle = shopify_handle(url)
    if not handle:
        return None
    origin = f"{urlparse(url).scheme or 'https'}://{urlparse(url).netloc}"
    try:
        r = httpx.get(f"{origin}/products/{handle}.js", timeout=timeout,
                      follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200 or "json" not in r.headers.get("content-type", "") \
                and "javascript" not in r.headers.get("content-type", ""):
            return None
        data = r.json()
        return data if isinstance(data, dict) and data.get("variants") else None
    except Exception as exc:
        log.debug("fetch_shopify_product failed for %s: %s", url, exc)
        return None


_SIZE_TOKEN_RE = re.compile(r"\b(\d{1,3}\s*[a-z]{1,3})\b", re.I)


def _size_tokens(text: str) -> list[str]:
    """Pull size-ish tokens like '34W', '30L', 'XL' out of free text, normalized (no spaces,
    lowercased): '34W x 30L' → ['34w', '30l']."""
    out = []
    for m in _SIZE_TOKEN_RE.finditer(text or ""):
        out.append(re.sub(r"\s+", "", m.group(1)).lower())
    return out


def match_variant(product: dict, variant_id: Optional[int] = None,
                  size_text: str = "") -> Optional[dict]:
    """Find the target variant in a Shopify product: by exact id (from ?variant=) first, else
    by matching ALL size tokens of size_text against the variant title ('34W x 30L' matches
    'Navy / 34W / 30L'). Returns the variant dict, or None."""
    variants = product.get("variants") or []
    if variant_id:
        for v in variants:
            if v.get("id") == variant_id:
                return v
    toks = _size_tokens(size_text)
    if toks:
        for v in variants:
            title = re.sub(r"\s+", "", (v.get("title") or "")).lower()
            if all(t in title for t in toks):
                return v
    return None


def check_restock(url: str, cfg, variant_id: Optional[int] = None,
                  size_text: str = "") -> dict:
    """Check whether a specific product/size is IN STOCK. Uses the Shopify data endpoint when
    the target is a Shopify product (exact, deterministic), and reports a structured state the
    scheduler compares run-to-run. Never raises.

    Returns: {method, ok, available, variant_title, price, note, url}. `ok` is False when we
    couldn't determine the state (so the caller neither alerts nor clears a prior state)."""
    variant_id = variant_id or _variant_id_from_url(url)
    product = fetch_shopify_product(url)
    if product is not None:
        v = match_variant(product, variant_id=variant_id, size_text=size_text)
        if v is None:
            avail_titles = [x.get("title") for x in product.get("variants", []) if x.get("available")]
            return {"method": "shopify", "ok": False, "available": None, "url": url,
                    "variant_title": "", "price": None,
                    "note": f"couldn't find that size on the page. In stock now: "
                            f"{', '.join(avail_titles) or 'none'}"}
        price = v.get("price")
        try:
            price = f"${int(price)/100:,.2f}" if price is not None else None
        except Exception:
            price = str(price)
        return {
            "method": "shopify", "ok": True, "available": bool(v.get("available")),
            "variant_title": v.get("title") or "", "price": price, "url": url,
            "note": f"{product.get('title','')} — {v.get('title','')}: "
                    f"{'IN STOCK' if v.get('available') else 'out of stock'}",
        }
    # Non-Shopify: the general LLM page-read fallback lands here (Phase 2).
    return {"method": "unsupported", "ok": False, "available": None, "url": url,
            "variant_title": "", "price": None,
            "note": "This isn't a Shopify product page — general restock reading is coming next."}
