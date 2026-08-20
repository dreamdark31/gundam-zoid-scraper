#!/usr/bin/env python3
"""
Gundam & Zoid price scraper.

Checks a list of search terms across several retailers and writes results to:
  - output/prices_YYYY-MM-DD.csv   every listing found today
  - output/latest.json             cheapest listing per search term
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
  - Every site here has its own Terms of Service, and several restrict
    automated access in some form. This script is written to be a polite,
    low-volume client (a few seconds between requests, one run a day) but
    that doesn't make it compliant with any given site's specific terms —
    that's worth checking yourself. Where a legitimate API exists (eBay)
    this script uses that instead of scraping HTML.
  - HLJ.com and HobbyLinkJapan.com may be the same store under two domains
    — worth checking before you count both toward the same "cheapest"
    comparison.
  - Amazon is intentionally left unimplemented. Its anti-bot systems are
    aggressive and scraping it violates its Conditions of Use. Use the
    Amazon Product Advertising API (requires an Associates account) if you
    need Amazon prices.
  - Mercari's site is heavily JavaScript-rendered, so plain requests +
    BeautifulSoup won't see search results. It's disabled by default; see
    the README for a Playwright-based approach if you want it added.
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
    )
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scraper")

DEFAULT_SHOPIFY_SELECTORS = {
    "item_selector": ".product-card, .card-wrapper, .grid__item, .product-item",
    "title_selector": ".card__heading a, .product-card__title, .product-item-meta__title, a.full-unstyled-link",
    "price_selector": ".price-item--regular, .price__current, .price, .money",
    "link_selector": "a[href]",
}

# ---------------------------------------------------------------------------
# Site definitions for the generic HTML scraper. Each entry needs:
#   name, search_url (with a {q} placeholder), base_url, currency
# and can override any of item_selector / title_selector / price_selector /
# link_selector — otherwise the Shopify-style defaults above are used.
# ALL selectors here are unverified guesses — see the module docstring.
# ---------------------------------------------------------------------------
SITE_DEFS = {
    "hobbylinkjapan": {
        "name": "HobbyLink Japan",
        "search_url": "https://www.hobbylinkjapan.com/search/?keywords={q}",
        "base_url": "https://www.hobbylinkjapan.com",
        "currency": "USD",
        "item_selector": ".item-cell, .product-item, .item",
        "title_selector": ".item-name, .product-name, a[title]",
        "price_selector": ".item-price, .price",
    },
    "bbts": {
        "name": "BigBadToyStore",
        "search_url": "https://www.bigbadtoystore.com/Search?SearchText={q}",
        "base_url": "https://www.bigbadtoystore.com",
        "currency": "USD",
        "item_selector": ".product-item, .search-result-item, .item-cell",
        "title_selector": ".product-title, .item-title, a[title]",
        "price_selector": ".product-price, .item-price, .price",
    },
    "mandarake": {
        "name": "Mandarake",
        "search_url": "https://order.mandarake.co.jp/order/listPage/list?keyword={q}&lang=en",
        "base_url": "https://order.mandarake.co.jp",
        "currency": "JPY",
        "item_selector": ".block, .itemBox, .item",
        "title_selector": ".title, .itemTitle, a[title]",
        "price_selector": ".price, .itemPrice",
    },
    "plazajapan": {
        "name": "Plaza Japan",
        "search_url": "https://www.plazajapan.com/catalogsearch/result/?q={q}",
        "base_url": "https://www.plazajapan.com",
        "currency": "USD",
        "item_selector": ".product-item, li.item",
        "title_selector": ".product-item-link, .product-name a",
        "price_selector": ".price",
    },
    "gundamplanet": {
        "name": "Gundam Planet",
        "search_url": "https://www.gundamplanet.com/search?q={q}&type=product",
        "base_url": "https://www.gundamplanet.com",
        "currency": "USD",
    },
    "hlj_com": {
        "name": "HLJ.com",
        "search_url": "https://www.hlj.com/catalogsearch/result/?q={q}",
        "base_url": "https://www.hlj.com",
        "currency": "USD",
        "item_selector": ".product-item, li.item",
        "title_selector": ".product-item-link, .product-name a",
        "price_selector": ".price",
    },
    "gundamplacestore": {
        "name": "Gundam Place Store",
        "search_url": "https://gundamplacestore.com/search?q={q}",
        "base_url": "https://gundamplacestore.com",
        "currency": "USD",
    },
    "otakumode": {
        "name": "Otaku Mode",
        "search_url": "https://otakumode.com/shop/search?q={q}",
        "base_url": "https://otakumode.com",
        "currency": "USD",
    },
    "1999": {
        "name": "1999.co.jp",
        "search_url": "https://www.1999.co.jp/eng/search?keyword={q}",
        "base_url": "https://www.1999.co.jp",
        "currency": "JPY",
        "item_selector": ".item, .productBlock, .searchResult .item",
        "title_selector": ".itemName, .productName, a[title]",
        "price_selector": ".price, .itemPrice",
    },
    "imageanime": {
        "name": "Image Anime",
        "search_url": "https://www.imageanime.com/search?q={q}",
        "base_url": "https://www.imageanime.com",
        "currency": "USD",
    },
    "usagundamstore": {
        "name": "USA Gundam Store",
        "search_url": "https://www.usagundamstore.com/search?q={q}",
        "base_url": "https://www.usagundamstore.com",
        "currency": "USD",
    },
    "gundamit": {
        "name": "GundamIT",
        "search_url": "https://gundamit.com/search?q={q}",
        "base_url": "https://gundamit.com",
        "currency": "USD",
    },
    "gundammodelcenter": {
        "name": "Gundam Model Center",
        "search_url": "https://www.gundammodelcenter.com/search?q={q}",
        "base_url": "https://www.gundammodelcenter.com",
        "currency": "USD",
    },
    "gundamcentralshop": {
        "name": "Gundam Central Shop",
        "search_url": "https://www.gundamcentralshop.com/search?q={q}",
        "base_url": "https://www.gundamcentralshop.com",
        "currency": "USD",
    },
}


def polite_sleep(lo=2.5, hi=5.5):
    time.sleep(random.uniform(lo, hi))


def get_soup(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as e:
        log.warning(f"  fetch failed for {url}: {e}")
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


def scrape_generic(term, site_key, site_cfg=None):
    site = SITE_DEFS[site_key]
    sel = dict(DEFAULT_SHOPIFY_SELECTORS)
    sel.update({k: v for k, v in site.items() if k.endswith("_selector")})

    url = site["search_url"].format(q=quote_plus(term))
    soup = get_soup(url)
    if soup is None:
        return []

    results = []
    for card in soup.select(sel["item_selector"]):
        title_el = card.select_one(sel["title_selector"])
        price_el = card.select_one(sel["price_selector"])
        link_el = card.select_one(sel["link_selector"])
        if not title_el or not price_el:
            continue
        price = parse_price(price_el.get_text())
        if price is None:
            continue
        href = link_el.get("href") if link_el else None
        if href and href.startswith("/"):
            href = site["base_url"] + href
        results.append({
            "site": site["name"],
            "search_term": term,
            "title": title_el.get_text(strip=True),
            "price": price,
            # Shipping cost almost never appears on a search-results page —
            # it's usually shown at checkout or on the product page. Left
            # null here; the tracker app lets a user fill it in by hand.
            "shipping": None,
            "currency": site.get("currency", "USD"),
            "condition": "New",
            "url": href,
        })
    return results


# ---------------------------------------------------------------------------
# eBay — uses the official Browse API (needs a free developer app id/cert id
# from developer.ebay.com). This is the one site scraped via API, not HTML,
# because eBay explicitly offers this for exactly this use case.
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


def scrape_ebay(term, cfg):
    if not cfg.get("app_id") or not cfg.get("cert_id"):
        log.info("  [ebay] skipped — no app_id/cert_id in config.json")
        return []
    try:
        token = get_ebay_token(cfg["app_id"], cfg["cert_id"])
        resp = requests.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": cfg.get("marketplace", "EBAY_US"),
            },
            params={"q": term, "limit": 20},
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("itemSummaries", [])
        results = []
        for it in items:
            price = it.get("price", {})
            # Browse API exposes shipping cost when it's a fixed amount;
            # "CALCULATED" or missing shippingCost means we can't know it
            # without picking a buyer location, so we leave it null rather
            # than guess.
            shipping = None
            for opt in it.get("shippingOptions", []):
                cost = opt.get("shippingCost", {}).get("value")
                if cost is not None:
                    shipping = parse_price(cost)
                    break
            results.append({
                "site": "eBay",
                "search_term": term,
                "title": it.get("title"),
                "price": parse_price(price.get("value")),
                "shipping": shipping,
                "currency": price.get("currency"),
                "condition": it.get("condition"),
                "url": it.get("itemWebUrl"),
            })
        return results
    except requests.RequestException as e:
        log.warning(f"  [ebay] request failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Mercari — disabled by default (JS-rendered search results; requests +
# BeautifulSoup can't see them). See README for a Playwright-based option.
# ---------------------------------------------------------------------------

def scrape_mercari(term, cfg):
    log.info("  [mercari] skipped — needs a JS-capable client, see README")
    return []


# ---------------------------------------------------------------------------
# Amazon — intentionally not implemented. See module docstring.
# ---------------------------------------------------------------------------

def scrape_amazon(term, cfg):
    log.info("  [amazon] skipped — not implemented, see README for why")
    return []


SCRAPERS = {"ebay": scrape_ebay, "mercari": scrape_mercari, "amazon": scrape_amazon}
for _key in SITE_DEFS:
    SCRAPERS[_key] = (lambda term, cfg, k=_key: scrape_generic(term, k, cfg))


def load_config():
    if not os.path.exists(CONFIG_PATH):
        log.error(f"Missing {CONFIG_PATH} — copy config.example.json to config.json and edit it.")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    # Allow eBay credentials to come from environment variables (e.g. GitHub
    # Actions secrets) instead of being committed to config.json.
    cfg.setdefault("ebay", {})
    if os.environ.get("EBAY_APP_ID"):
        cfg["ebay"]["app_id"] = os.environ["EBAY_APP_ID"]
    if os.environ.get("EBAY_CERT_ID"):
        cfg["ebay"]["cert_id"] = os.environ["EBAY_CERT_ID"]
    return cfg


def run():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    cfg = load_config()
    terms = cfg.get("search_terms", [])
    if not terms:
        log.error("config.json has no search_terms — add at least one kit name.")
        sys.exit(1)

    all_results = []
    for term in terms:
        log.info(f"Searching: {term}")
        for site_key, fn in SCRAPERS.items():
            site_cfg = cfg.get(site_key, {})
            if not site_cfg.get("enabled", False):
                continue
            log.info(f"  -> {site_key}")
            results = fn(term, site_cfg)
            log.info(f"     {len(results)} result(s)")
            all_results.extend(results)
            polite_sleep()

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    generated_at = datetime.now(timezone.utc).isoformat()

    csv_path = os.path.join(OUTPUT_DIR, f"prices_{date_str}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["search_term", "site", "title", "price", "currency", "condition", "url"]
        )
        writer.writeheader()
        for r in all_results:
            writer.writerow({k: r.get(k, "") for k in writer.fieldnames})
    log.info(f"Wrote {len(all_results)} rows to {csv_path}")

    # Full flat list — this is what the tracker app's "Refresh Prices"
    # button fetches and matches against each kit's match term.
    full_path = os.path.join(OUTPUT_DIR, "latest_full.json")
    with open(full_path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": generated_at, "results": all_results}, f, indent=2)
    log.info(f"Wrote {full_path}")

    # Rolling summary: cheapest listing per search term, for quick reading.
    cheapest = {}
    for r in all_results:
        if r.get("price") is None:
            continue
        term = r["search_term"]
        if term not in cheapest or r["price"] < cheapest[term]["price"]:
            cheapest[term] = r
    summary_path = os.path.join(OUTPUT_DIR, "latest.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": generated_at, "cheapest_by_term": cheapest}, f, indent=2)
    log.info(f"Wrote summary to {summary_path}")

    if cheapest:
        log.info("Cheapest found this run:")
        for term, r in cheapest.items():
            log.info(f"  {term}: {r['price']} {r.get('currency','')} at {r['site']} — {r.get('url','')}")
    else:
        log.warning(
            "No prices found at all. Most likely a selector is stale — "
            "open one of the site's search pages by hand and compare its HTML "
            "to the site's entry in SITE_DEFS."
        )


if __name__ == "__main__":
    run()
