#!/usr/bin/env python3
"""
Gundam & Zoid price scraper — full-catalog mode.

Instead of searching for specific kit names, this browses each site's
whole Gundam catalog and whole Zoids catalog (paginating through search
results) so you get pricing on everything a site carries, not just kits
you've named ahead of time. Writes:

  - output/prices_YYYY-MM-DD.csv   every listing found today
  - output/latest.json             cheapest listing per category (gundam/zoid)
  - output/latest_full.json        every listing found today, flat list —
                                    this is the file the tracker app's
                                    "Refresh Prices" button reads

IMPORTANT — READ BEFORE SCHEDULING THIS TO RUN DAILY:
  - Site HTML changes over time, and I wrote every CSS selector below from
    general knowledge of how these storefronts are typically built — I have
    no network access to load the real pages and check. Treat every entry
    in SITE_DEFS as an unverified starting point. Run once by hand
    (`python scraper.py`), check output/prices_*.csv, and fix any selector
    that comes back with 0 results by inspecting the real page (right-click
    a product tile -> Inspect) before you schedule this to run unattended.
  - Pagination is guessed too (a `?page=N`-style parameter appended to the
    search URL). Some sites use a different scheme (offset-based, "load
    more" via JavaScript, etc.) and will just return the same first page
    repeatedly, or nothing, past page 1 — verify this per site the same
    way as selectors.
  - Browsing an entire catalog is a much bigger crawl than searching one
    term at a time — more requests per site, per run. `max_pages_per_site`
    in config.json caps this; keep it conservative and raise it gradually
    rather than starting at a large number.
  - Every site here has its own Terms of Service, and several restrict
    automated access in some form — this applies more, not less, once
    you're crawling a whole catalog instead of a single search. Where a
    legitimate API exists (eBay) this script uses that instead of
    scraping HTML.
  - HLJ.com and HobbyLinkJapan.com may be the same store under two domains
    — worth checking before you count both toward the same comparison.
  - Amazon is intentionally left unimplemented (aggressive anti-bot,
    Conditions of Use restrict scraping — use the Product Advertising API
    instead if you need Amazon prices). Mercari is disabled by default —
    its search results are JS-rendered and invisible to requests +
    BeautifulSoup; see the README for a Playwright-based option.
"""

import csv
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    # A more complete, browser-like header set — this is a best-effort
    # attempt to look less like a bot to sites behind Cloudflare or
    # similar protection (BigBadToyStore, Otaku Mode, 1999.co.jp all
    # returned 403 with the bare User-Agent alone). It is NOT guaranteed
    # to work: real bot-detection systems also look at TLS/HTTP
    # fingerprints that the `requests` library can't fully disguise as a
    # real browser, so a 403 may persist for some sites no matter what
    # headers are sent. If a site keeps 403ing after this change, that's
    # the likely ceiling of what plain HTTP requests can do against it.
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scraper")

DEFAULT_SHOPIFY_SELECTORS = {
    "item_selector": ".product-card, .card-wrapper, .grid__item, .product-item",
    # A LIST, tried in order, not a single comma-joined string — comma
    # selectors don't have priority, select_one() just returns whichever
    # match appears first in the actual HTML. On a Dawn-theme card (real
    # USAGundamStore HTML, confirmed) that's the empty image link, not the
    # title, so each of these is tried as its own separate query until one
    # returns non-empty text.
    "title_selector": [
        ".card-information__text a",
        ".card__heading a",
        ".product-card__title",
        ".product-item-meta__title",
        "a.full-unstyled-link",
    ],
    # Sale price is tried first (see scrape_generic_page) because Dawn-theme
    # cards keep the crossed-out original price in the DOM even when an
    # item is on sale, positioned before the actual sale price — a plain
    # "first price element" selector would silently return the wrong,
    # higher price for anything discounted.
    "sale_price_selector": ".price-item--sale",
    "price_selector": ".price-item--regular, .price__current, .price, .money",
    "link_selector": "a[href]",
    "image_selector": "img",
    "page_param": "page",
}

def select_first(card, selectors):
    """Try each selector in order (a list — priority matters), or fall back
    to a single select_one() call if given a plain comma-joined string."""
    if isinstance(selectors, str):
        return card.select_one(selectors)
    for sel_str in selectors:
        el = card.select_one(sel_str)
        if el and el.get_text(strip=True):
            return el
    return None


def extract_image_url(card, selector, base_url):
    """Find a product thumbnail and return an absolute URL, or None. Tries
    data-src before src (many sites keep the real image behind data-src for
    lazy loading, with src holding a blank placeholder until JS runs — since
    this scraper doesn't run JS, we want whichever attribute actually has
    real content). Handles protocol-relative URLs (//cdn.example.com/...)
    and root-relative ones (/images/...)."""
    img = card.select_one(selector) if selector else None
    if img is None:
        return None
    url = img.get("data-src") or img.get("src") or ""
    url = url.strip()
    if not url or url.startswith("data:"):  # blank/placeholder pixel
        return None
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return base_url + url
    return url

# ---------------------------------------------------------------------------
# Site definitions for the generic HTML scraper. Each entry needs:
#   name, search_url (with a {q} placeholder), base_url, currency
# and can override any of item_selector / title_selector / price_selector /
# link_selector / page_param — otherwise the Shopify-style defaults above
# are used. ALL selectors and pagination params here are unverified
# guesses — see the module docstring.
# ---------------------------------------------------------------------------
SITE_DEFS = {
    "bbts": {
        "name": "BigBadToyStore",
        "site_type": "retailer",
        "search_url": "https://www.bigbadtoystore.com/Search?SearchText={q}",
        "base_url": "https://www.bigbadtoystore.com",
        "currency": "USD",
        "item_selector": ".product-item, .search-result-item, .item-cell",
        "title_selector": ".product-title, .item-title, a[title]",
        "price_selector": ".product-price, .item-price, .price",
        "page_param": "Page",
    },
    "mandarake": {
        "name": "Mandarake",
        "site_type": "marketplace",
        "search_url": "https://order.mandarake.co.jp/order/listPage/list?keyword={q}&lang=en",
        "base_url": "https://order.mandarake.co.jp",
        "currency": "JPY",
        "item_selector": ".block, .itemBox, .item",
        "title_selector": ".title, .itemTitle, a[title]",
        "price_selector": ".price, .itemPrice",
    },
    "plazajapan": {
        "name": "Plaza Japan",
        "site_type": "retailer",
        "search_url": "https://www.plazajapan.com/catalogsearch/result/?q={q}",
        "base_url": "https://www.plazajapan.com",
        "currency": "USD",
        "item_selector": ".product-item, li.item",
        "title_selector": ".product-item-link, .product-name a",
        "price_selector": ".price",
        "page_param": "p",
    },
    "gundamplanet": {
        "name": "Gundam Planet",
        "site_type": "retailer",
        "search_url": "https://www.gundamplanet.com/search?q={q}&type=product",
        "base_url": "https://www.gundamplanet.com",
        "currency": "USD",
    },
    "hlj_com": {
        "name": "HLJ.com",
        "site_type": "retailer",
        # URL and pagination param confirmed against a real search results
        # page (https://www.hlj.com/search/?Word=gundam&Page=1) — the
        # earlier Magento-style guess (catalogsearch/result, ?q=) was wrong,
        # that's why this 404'd. Item/title/price selectors below are
        # STILL unverified guesses — I could see product titles and images
        # in the page's server-rendered content, but not clear price text
        # in the same fetch, which raises the possibility price is loaded
        # separately via JavaScript on this site rather than being in the
        # initial HTML. If this comes back with titles but no prices (or
        # 0 results) after the URL fix, that's the likely reason — send
        # real product-tile HTML the same way as for other sites and,
        # importantly, check with "View Page Source" (not just Inspect)
        # whether a price number actually appears in the raw HTML at all.
        "search_url": "https://www.hlj.com/search/?Word={q}",
        "base_url": "https://www.hlj.com",
        "currency": "USD",
        "item_selector": ".product-item, li.item",
        "title_selector": ".product-item-link, .product-name a",
        "price_selector": ".price",
        "page_param": "Page",
    },
    "gundamplacestore": {
        "name": "Gundam Place Store",
        "site_type": "retailer",
        "search_url": "https://gundamplacestore.com/search?q={q}",
        "base_url": "https://gundamplacestore.com",
        "currency": "USD",
    },
    "otakumode": {
        "name": "Otaku Mode",
        "site_type": "retailer",
        "search_url": "https://otakumode.com/shop/search?q={q}",
        "base_url": "https://otakumode.com",
        "currency": "USD",
    },
    "1999": {
        "name": "1999.co.jp",
        "site_type": "retailer",
        "search_url": "https://www.1999.co.jp/eng/search?keyword={q}",
        "base_url": "https://www.1999.co.jp",
        "currency": "JPY",
        "item_selector": ".item, .productBlock, .searchResult .item",
        "title_selector": ".itemName, .productName, a[title]",
        "price_selector": ".price, .itemPrice",
    },
    "imageanime": {
        # Confirmed this is an old-style hand-built site (category pages
        # like /gundam.html, /gunplasmodki.html, "Website Design by
        # Yahoo!" in the footer) — not a modern storefront with a
        # /search?q= endpoint like the guess assumed, which is why it
        # 404'd. It has no obvious search URL to plug in here at all;
        # scraping it would mean crawling its category pages instead of
        # searching, which is a different approach than every other site
        # in this file. Disabled until that's built — set enabled: true
        # in config.json only once that exists.
        "name": "Image Anime",
        "site_type": "retailer",
        "search_url": "https://www.imageanime.com/search?q={q}",
        "base_url": "https://www.imageanime.com",
        "currency": "USD",
    },
    "usagundamstore": {
        # Confirmed via the site's Network tab: search results are loaded
        # by JavaScript from Searchspring (a third-party search platform),
        # not present in the page's own HTML — so this uses a dedicated
        # JSON API backend (scrape_searchspring_catalog) instead of the
        # generic HTML scraper. Field names (name/price/url/imageUrl)
        # confirmed against a real response.
        "name": "USA Gundam Store",
        "site_type": "retailer",
        "backend": "searchspring",
        "searchspring_site_id": "ckt36l",
        "base_url": "https://www.usagundamstore.com",
        "currency": "USD",
    },
    "gundamit": {
        "name": "GundamIT",
        "site_type": "retailer",
        "search_url": "https://gundamit.com/search?q={q}",
        "base_url": "https://gundamit.com",
        "currency": "USD",
    },
    "gundammodelcenter": {
        "name": "Gundam Model Center",
        "site_type": "retailer",
        "search_url": "https://www.gundammodelcenter.com/search?q={q}",
        "base_url": "https://www.gundammodelcenter.com",
        "currency": "USD",
    },
    "gundamcentralshop": {
        "name": "Gundam Central Shop",
        "site_type": "retailer",
        "search_url": "https://www.gundamcentralshop.com/search?q={q}",
        "base_url": "https://www.gundamcentralshop.com",
        "currency": "USD",
    },
    "kotobukiya": {
        "name": "Kotobukiya USA",
        # Real domain and selectors confirmed via View Page Source against
        # a real search results page (server-rendered, not JS — a genuine
        # selector/URL issue, not the rendering issue USAGundamStore had).
        # The URL below matches the exact confirmed-working search URL —
        # a bare "?q=..." wasn't enough; this theme's search apparently
        # needs the extra params too (options[prefix]=last,
        # filter.p.product_type=) to return the same server-rendered
        # results page a browser gets.
        "search_url": "https://kotobukiya-us.com/search?options%5Bprefix%5D=last&q={q}&filter.p.product_type=",
        "base_url": "https://kotobukiya-us.com",
        "currency": "USD",
        "site_type": "retailer",
        "item_selector": "product-card",
        "title_selector": [".card__title a"],
        "price_selector": ".price__current",
        "link_selector": "a.card-link, a[href]",
        "image_selector": "img",
    },
}

# ---------------------------------------------------------------------------
# Non-kit filter. eBay's category restriction (above) keeps its results to
# actual model kits, but the HTML-scraped retail sites have no equivalent
# category API — a plain "gundam"/"zoids" search on them can still surface
# trading cards, plush, keychains, apparel, and similar merch alongside
# actual kits. This is a best-effort keyword blacklist applied to every
# site's results, not a guarantee — tune the list below if you spot either
# false positives (a real kit getting filtered out) or false negatives
# (junk still getting through).
# ---------------------------------------------------------------------------
NON_KIT_KEYWORDS = [
    "trading card", "tcg", "ccg", "card game", "playing card", "carddass",
    "card set", "booster pack", "booster box",
    "sticker", "stickers", "decal sheet",
    "keychain", "key chain", "keyring", "key ring", "lanyard",
    "acrylic stand", "acrylic keychain", "nendoroid",
    "plush", "plushie", "stuffed",
    "poster", "wall scroll", "postcard", "calendar",
    "t-shirt", "tshirt", "hoodie", "sweatshirt", "apparel",
    "mug", "tumbler", "coaster",
    "patch", "pin badge", "enamel pin", "button badge",
    "phone case", "wallet", "tote bag", "backpack",
    "notebook", "bookmark", "clear file", "folder",
    "artbook", "art book", "guide book", "magazine",
    "jigsaw puzzle",
]


def is_probably_kit(title):
    if not title:
        return True  # don't drop items we can't evaluate
    t = title.lower()
    return not any(kw in t for kw in NON_KIT_KEYWORDS)


def polite_sleep(lo=2.5, hi=5.5):
    time.sleep(random.uniform(lo, hi))


def get_soup(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as e:
        log.warning(f"    fetch failed for {url}: {e}")
        return None


def parse_price(text):
    """Pull the first plausible number out of a price string like '$54.99' or '¥6,980'."""
    if not text:
        return None
    cleaned = "".join(c for c in text if c.isdigit() or c in ".,")
    cleaned = cleaned.replace(",", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def scrape_generic_page(query, site_key, page):
    """Fetch one page of a site's search results for `query`. Returns a list
    of raw listings (no category attached yet)."""
    site = SITE_DEFS[site_key]
    sel = dict(DEFAULT_SHOPIFY_SELECTORS)
    sel.update({k: v for k, v in site.items() if k.endswith("_selector") or k == "page_param"})

    url = site["search_url"].format(q=quote_plus(query))
    if page > 1:
        url += f"&{sel['page_param']}={page}"
    soup = get_soup(url)
    if soup is None:
        return []

    results = []
    for card in soup.select(sel["item_selector"]):
        title_el = select_first(card, sel["title_selector"])
        # Prefer the sale price if the card has one (see comment on
        # sale_price_selector above) — otherwise fall back to the regular
        # price selector. This order matters: checking regular first would
        # return the crossed-out original price on discounted items.
        price_el = None
        if sel.get("sale_price_selector"):
            price_el = select_first(card, sel["sale_price_selector"])
        if price_el is None:
            price_el = select_first(card, sel["price_selector"])
        link_el = card.select_one(sel["link_selector"])
        if not title_el or not price_el:
            continue
        price = parse_price(price_el.get_text())
        if price is None:
            continue
        href = link_el.get("href") if link_el else None
        if href and href.startswith("/"):
            href = site["base_url"] + href
        image_url = extract_image_url(card, sel.get("image_selector"), site["base_url"])
        results.append({
            "site": site["name"],
            "site_type": site.get("site_type", "retailer"),
            "title": title_el.get_text(strip=True),
            "price": price,
            "shipping": None,  # search-results pages almost never show this
            "currency": site.get("currency", "USD"),
            "condition": "New",
            "url": href,
            "image_url": image_url,
        })
    return results


def scrape_generic_catalog(query, site_key, max_pages):
    """Page through a site's search results for `query` until a page comes
    back empty or max_pages is hit."""
    all_results = []
    seen_urls = set()
    for page in range(1, max_pages + 1):
        page_results = scrape_generic_page(query, site_key, page)
        if not page_results:
            break  # no more pages (or the selector never matched anything)
        new_count = 0
        for r in page_results:
            key = r.get("url") or (r["site"], r["title"], r["price"])
            if key in seen_urls:
                continue
            seen_urls.add(key)
            all_results.append(r)
            new_count += 1
        if new_count == 0:
            break  # pagination isn't advancing — same page repeating
        if page < max_pages:
            polite_sleep()
    return all_results


# ---------------------------------------------------------------------------
# Searchspring — a third-party search platform some Shopify stores plug in
# instead of Shopify's own search. When that's the case, the storefront's
# own search results page is JS-rendered and invisible to a plain HTML
# scrape (confirmed via "View Page Source" showing no product data at all
# for USAGundamStore) — but Searchspring's search API itself is public
# JSON, documented at docs.searchspring.com, and far more reliable to
# parse than guessing HTML selectors. If another site turns out to use
# Searchspring too (check its Network tab for a *.searchspring.io request
# the same way), give it "backend": "searchspring" and a
# "searchspring_site_id" in SITE_DEFS instead of writing new selectors.
# ---------------------------------------------------------------------------

def scrape_searchspring_page(query, site, page, results_per_page=48):
    site_id = site["searchspring_site_id"]
    # Searchspring's API generally doesn't require an Origin/Referer to
    # return results (it's designed for third-party checkout/AMP contexts
    # too), but sending them anyway costs nothing and rules this out as a
    # cause if results are still empty after this change.
    ss_headers = dict(HEADERS)
    ss_headers["Referer"] = site["base_url"] + "/"
    ss_headers["Origin"] = site["base_url"]
    try:
        resp = requests.get(
            f"https://{site_id}.a.searchspring.io/api/search/search.json",
            headers=ss_headers,
            params={
                "siteId": site_id,
                "q": query,
                "resultsFormat": "native",
                "page": page,
                "resultsPerPage": results_per_page,
            },
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning(f"    fetch failed for Searchspring ({site['name']}): {e}")
        return [], 0

    # requests.exceptions.JSONDecodeError is (confusingly) ALSO a
    # RequestException in current requests versions, so it would have
    # been swallowed by the block above with a useless message — this is
    # deliberately its own try/except, after the request itself already
    # succeeded, so we always get the actual status code and body.
    try:
        data = resp.json()
    except ValueError:
        log.warning(
            f"    Searchspring ({site['name']}) returned non-JSON — "
            f"status {resp.status_code}, {len(resp.content)} bytes. "
            f"First 200 chars of body: {resp.text[:200]!r}"
        )
        return [], 0

    items = data.get("results", [])
    total_pages = data.get("pagination", {}).get("totalPages", page)
    total_results = data.get("pagination", {}).get("totalResults")
    if not items:
        log.warning(
            f"    Searchspring ({site['name']}) page {page}: 0 items in response. "
            f"pagination.totalResults={total_results!r}, top-level keys={list(data.keys())}"
        )

    results = []
    for it in items:
        price = parse_price(it.get("price"))
        if price is None:
            continue
        url = it.get("url") or ""
        if url.startswith("/"):
            url = site["base_url"] + url
        results.append({
            "site": site["name"],
            "site_type": site.get("site_type", "retailer"),
            "title": it.get("name", ""),
            "price": price,
            "shipping": None,
            "currency": site.get("currency", "USD"),
            "condition": "New",
            "url": url,
            "image_url": it.get("imageUrl"),
        })
    return results, total_pages


def scrape_searchspring_catalog(query, site_key, max_pages):
    site = SITE_DEFS[site_key]
    all_results = []
    for page in range(1, max_pages + 1):
        page_results, total_pages = scrape_searchspring_page(query, site, page)
        if not page_results:
            break
        all_results.extend(page_results)
        if page >= total_pages:
            break
        if page < max_pages:
            polite_sleep()
    return all_results


# ---------------------------------------------------------------------------
# eBay — uses the official Browse API (needs a free developer app id/cert id
# from developer.ebay.com), paginated via offset until `max_results` is hit
# or eBay reports no more items.
# ---------------------------------------------------------------------------

def get_ebay_token(app_id, cert_id):
    import base64
    creds = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
    resp = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def scrape_ebay_catalog(query, cfg, max_results):
    if not cfg.get("app_id") or not cfg.get("cert_id"):
        log.info("    [ebay] skipped — no app_id/cert_id in config.json")
        return []
    try:
        token = get_ebay_token(cfg["app_id"], cfg["cert_id"])
    except requests.RequestException as e:
        log.warning(f"    [ebay] auth failed: {e}")
        return []

    # Restrict to eBay's "Models & Kits" category (under Toys & Hobbies) so
    # results are actual model kits rather than trading cards, plush,
    # keychains, etc. that also turn up on a plain "gundam"/"zoids" text
    # search. Category ID confirmed against eBay's own category browse
    # URLs (https://www.ebay.com/b/Toy-Models-Kits/1188/...) — override via
    # config.json's "category_id" under "ebay" if this ever changes or if
    # you want a different category.
    category_id = cfg.get("category_id", "1188")

    results = []
    offset = 0
    page_size = 50
    while offset < max_results:
        try:
            resp = requests.get(
                "https://api.ebay.com/buy/browse/v1/item_summary/search",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-EBAY-C-MARKETPLACE-ID": cfg.get("marketplace", "EBAY_US"),
                },
                params={
                    "q": query,
                    "category_ids": category_id,
                    "limit": min(page_size, max_results - offset),
                    "offset": offset,
                },
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning(f"    [ebay] request failed: {e}")
            break

        try:
            data = resp.json()
        except ValueError:
            log.warning(f"    [ebay] non-JSON response (status {resp.status_code}): {resp.text[:200]!r}")
            break
        items = data.get("itemSummaries", [])
        if not items:
            break
        for it in items:
            price = it.get("price", {})
            shipping = None
            for opt in it.get("shippingOptions", []):
                cost = opt.get("shippingCost", {}).get("value")
                if cost is not None:
                    shipping = parse_price(cost)  # 0.0 here means confirmed free shipping
                    break
            # eBay's Browse API documents a "pickupOptions" array on
            # ItemSummary for listings that offer local/arranged pickup —
            # I haven't been able to verify this against a live response
            # (no network access here), so treat this as best-effort: if
            # it turns out to always be empty/missing even for listings
            # you know offer pickup, tell me and I'll adjust.
            pickup = bool(it.get("pickupOptions"))
            results.append({
                "site": "eBay",
                "site_type": "marketplace",
                "title": it.get("title"),
                "price": parse_price(price.get("value")),
                "shipping": shipping,
                "pickup": pickup,
                "currency": price.get("currency"),
                "condition": it.get("condition"),
                "url": it.get("itemWebUrl"),
                "image_url": it.get("image", {}).get("imageUrl"),
            })

        offset += len(items)
        if offset >= data.get("total", 0):
            break
        polite_sleep()

    return results


def scrape_mercari_catalog(query, cfg, max_pages):
    log.info("    [mercari] skipped — needs a JS-capable client, see README")
    return []


def scrape_amazon_catalog(query, cfg, max_pages):
    log.info("    [amazon] skipped — not implemented, see README for why")
    return []


def load_config():
    if not os.path.exists(CONFIG_PATH):
        log.error(f"Missing {CONFIG_PATH} — copy config.example.json to config.json and edit it.")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg.setdefault("ebay", {})
    if os.environ.get("EBAY_APP_ID"):
        cfg["ebay"]["app_id"] = os.environ["EBAY_APP_ID"]
    if os.environ.get("EBAY_CERT_ID"):
        cfg["ebay"]["cert_id"] = os.environ["EBAY_CERT_ID"]
    return cfg


def run():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    cfg = load_config()
    categories = cfg.get("categories", {})
    if not categories:
        log.error('config.json has no "categories" — e.g. {"gundam": "gundam", "zoid": "zoids"}')
        sys.exit(1)
    max_pages = cfg.get("max_pages_per_site", 3)
    max_ebay_results = cfg.get("ebay", {}).get("max_results", 150)

    all_results = []
    for category, query in categories.items():
        log.info(f"Category: {category}  (query: \"{query}\")")

        ebay_cfg = cfg.get("ebay", {})
        if ebay_cfg.get("enabled", False):
            log.info("  -> ebay")
            try:
                results = scrape_ebay_catalog(query, ebay_cfg, max_ebay_results)
            except Exception as e:
                log.error(f"     unexpected error scraping ebay, skipping it: {e}")
                results = []
            kept = [r for r in results if is_probably_kit(r.get("title"))]
            dropped = len(results) - len(kept)
            log.info(f"     {len(kept)} result(s)" + (f"  ({dropped} filtered as non-kit)" if dropped else ""))
            for r in kept:
                r["category"] = category
                r["query"] = query
            all_results.extend(kept)
            polite_sleep()

        mercari_cfg = cfg.get("mercari", {})
        if mercari_cfg.get("enabled", False):
            log.info("  -> mercari")
            scrape_mercari_catalog(query, mercari_cfg, max_pages)

        amazon_cfg = cfg.get("amazon", {})
        if amazon_cfg.get("enabled", False):
            log.info("  -> amazon")
            scrape_amazon_catalog(query, amazon_cfg, max_pages)

        for site_key in SITE_DEFS:
            site_cfg = cfg.get(site_key, {})
            if not site_cfg.get("enabled", False):
                continue
            log.info(f"  -> {site_key}")
            # A bug or an unexpected response shape in any one site's
            # scraper must never be able to kill the whole run and lose
            # every other site's (and every other category's) results
            # collected so far — that's exactly what happened when an
            # unguarded resp.json() call crashed the entire script. Every
            # per-site call is now isolated like this.
            try:
                if SITE_DEFS[site_key].get("backend") == "searchspring":
                    results = scrape_searchspring_catalog(query, site_key, max_pages)
                else:
                    results = scrape_generic_catalog(query, site_key, max_pages)
            except Exception as e:
                log.error(f"     unexpected error scraping {site_key}, skipping it: {e}")
                results = []
            kept = [r for r in results if is_probably_kit(r.get("title"))]
            dropped = len(results) - len(kept)
            log.info(f"     {len(kept)} result(s) across up to {max_pages} page(s)" + (f"  ({dropped} filtered as non-kit)" if dropped else ""))
            for r in kept:
                r["category"] = category
                r["query"] = query
            all_results.extend(kept)
            polite_sleep()

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    generated_at = datetime.now(timezone.utc).isoformat()

    csv_path = os.path.join(OUTPUT_DIR, f"prices_{date_str}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["category", "site", "site_type", "title", "price", "shipping", "pickup", "currency", "condition", "url", "image_url"]
        )
        writer.writeheader()
        for r in all_results:
            writer.writerow({k: r.get(k, "") for k in writer.fieldnames})
    log.info(f"Wrote {len(all_results)} rows to {csv_path}")

    full_path = os.path.join(OUTPUT_DIR, "latest_full.json")
    with open(full_path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": generated_at, "results": all_results}, f, indent=2)
    log.info(f"Wrote {full_path}")

    cheapest = {}
    for r in all_results:
        if r.get("price") is None:
            continue
        cat = r["category"]
        if cat not in cheapest or r["price"] < cheapest[cat]["price"]:
            cheapest[cat] = r
    summary_path = os.path.join(OUTPUT_DIR, "latest.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": generated_at, "cheapest_by_category": cheapest}, f, indent=2)
    log.info(f"Wrote summary to {summary_path}")

    log.info(f"Total listings this run: {len(all_results)}")
    if cheapest:
        for cat, r in cheapest.items():
            log.info(f"  cheapest {cat}: {r['price']} {r.get('currency','')} at {r['site']} — {r.get('title','')}")
    else:
        log.warning(
            "No prices found at all. Most likely a selector is stale — "
            "open one of the site's search pages by hand and compare its HTML "
            "to the site's entry in SITE_DEFS."
        )


if __name__ == "__main__":
    run()
