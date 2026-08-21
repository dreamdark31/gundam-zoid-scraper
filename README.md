# Gundam & Zoid price scraper

Browses each site's whole Gundam catalog and whole Zoids catalog — not
just kits you've named ahead of time — by paging through broad search
results, and writes:

- `output/prices_YYYY-MM-DD.csv` — every listing found that day
- `output/latest.json` — the cheapest listing per category (gundam/zoid)
- `output/latest_full.json` — every listing found that day, as a flat list —
  **this is the file the tracker app's "Refresh Prices" button reads**

This is a much bigger crawl than searching one kit name at a time — every
site gets paged through (`max_pages_per_site` pages deep) for both
categories, every run. Start with a small page count and raise it once
you've confirmed a site's pagination actually works (see below).

## Sites covered

Each site is also tagged `retailer` or `marketplace` in the output
(`site_type` field) — retailers sell new stock directly, marketplaces are
resale/auction/mixed-seller platforms. This lets the tracker app filter
"official retailers" separately from "resale/marketplace" listings.

| Site | Type | Method | Status |
|---|---|---|---|
| eBay | marketplace | Official Browse API | Needs a free API key (see below) |
| HobbyLink Japan | retailer | HTML scrape | Enabled — **verify selectors first** |
| BigBadToyStore | retailer | HTML scrape | Enabled — **verify selectors first** |
| Mandarake | marketplace | HTML scrape | Enabled — **verify selectors first** |
| Plaza Japan | retailer | HTML scrape | Enabled — **verify selectors first** |
| Gundam Planet | retailer | HTML scrape | Enabled — **verify selectors first** |
| HLJ.com | retailer | HTML scrape | Enabled — **verify selectors first**, see note below |
| Gundam Place Store | retailer | HTML scrape | Enabled — **verify selectors first** |
| Otaku Mode | retailer | HTML scrape | Enabled — **verify selectors first** |
| 1999.co.jp | retailer | HTML scrape | Enabled — **verify selectors first** |
| Image Anime | retailer | HTML scrape | Enabled — **verify selectors first** |
| USA Gundam Store | retailer | HTML scrape | Enabled — **verify selectors first** |
| GundamIT | retailer | HTML scrape | Enabled — **verify selectors first** |
| Gundam Model Center | retailer | HTML scrape | Enabled — **verify selectors first** |
| Gundam Central Shop | retailer | HTML scrape | Enabled — **verify selectors first** |
| Kotobukiya USA | retailer | HTML scrape | Enabled — **verify selectors first**, URL guessed, see note below |
| Mercari | marketplace | — | Disabled — site is JS-rendered, see below |
| Amazon | marketplace | — | Disabled on purpose, see below |

### Heads up: Kotobukiya's URL is a guess

I don't have a confirmed URL for Kotobukiya's US storefront — I used
`kotobukiya-shop.com` as a best guess, but their actual shop domain may be
different (they also sell through hobbylinkjapan.com and other retailers
listed here in some regions). Check this one against the real site before
relying on it; if the domain's wrong the request will just fail cleanly
and log 0 results rather than error out.

### Heads up: HLJ.com vs. HobbyLink Japan

`hlj.com` and `hobbylinkjapan.com` may well be the same retailer under two
domains — I couldn't check from here. Worth confirming before you rely on
both as independent price points; if they're the same store, disable one
of them in `config.json` so it doesn't double-count in comparisons.

### New sites are unverified guesses, more so than the original four

The five sites added in the first pass (HLJ, BBTS, Mandarake, Plaza Japan)
already needed selector verification. The ten sites added after that are
in the same boat but I have even less specific knowledge of their exact
page structure — I used generic Shopify-storefront defaults
(`.product-card`, `.price`, etc.) for most of them since that's a common
platform for small hobby shops, but several may run something else
entirely (WooCommerce, a custom cart) with different markup. Budget time
to check each one against the real page before trusting its output.

### Why the HTML scrapers need "verifying"

I wrote the CSS selectors (`.select(...)` calls in `scraper.py`) from general
knowledge of how these sites are typically structured, but I have no network
access to load the actual pages and check. Site markup changes and I can't
guarantee these are correct today. **Run the script once by hand first**:

```
python scraper.py
```

Then open `output/prices_*.csv`. If a site returns 0 results, open its
search page in your own browser, right-click a product tile → Inspect, and
update the matching `.select(...)` line in `scraper.py` to match the real
class names. This is the normal maintenance loop for any scraper — sites
redesign their pages every so often and selectors need the occasional fix.

### Pagination is also a guess

Each site in `SITE_DEFS` has a `page_param` (e.g. `page`, `p`) appended to
the search URL as `&page=2`, `&page=3`, etc. Some sites use a different
scheme entirely (an offset number, a "load more" button driven by
JavaScript with no real page-N URL at all) — for those, every "page" will
just return the same first-page results, and `scrape_generic_catalog`
will stop after the first page once it notices nothing new is coming in.
That's a safe failure mode (you don't get duplicates), but it does mean
you're only getting page 1 of that site's catalog until the pagination
scheme is fixed to match reality.

### Why Amazon isn't implemented

Amazon's anti-bot systems are aggressive and its Conditions of Use restrict
automated scraping. If you want Amazon prices, use the official [Product
Advertising API](https://webservices.amazon.com/paapi5/documentation/)
(requires an Amazon Associates account) rather than scraping HTML.

### Why Mercari isn't implemented

Mercari's search results are loaded via JavaScript after the page loads, so
`requests` + `BeautifulSoup` only ever sees an empty shell. Getting real
results needs a browser-automation tool like
[Playwright](https://playwright.dev/python/) instead. If you want this
added, say so and I'll write it — it's a different approach (spins up a
headless browser) rather than a tweak to the existing function.

## Setup

```bash
cd gundam-zoid-scraper
pip install -r requirements.txt
cp config.example.json config.json
```

Edit `config.json`:
- `categories`: maps a category label to the broad search term used for
  it, e.g. `{"gundam": "gundam", "zoid": "zoids"}`. Change the query text
  if a site's search works better with something more specific — the
  label on the left (`gundam`/`zoid`) is what the tracker app filters by,
  the text on the right is what actually gets searched.
- `max_pages_per_site`: how deep to page into each site's results, per
  category, per run. Start at 2–3 and raise it once pagination is
  confirmed working for a given site (see above).
- Each site has an `"enabled": true/false` flag — turn off any you don't want.
- `ebay.max_results`: cap on how many eBay listings to pull per category
  per run (paginated via the API, 50 at a time).

### Getting an eBay API key (free)

1. Create an account at [developer.ebay.com](https://developer.ebay.com/).
2. Create a "Production" keyset for an application.
3. Copy the App ID and Cert ID into `config.json` under `"ebay"`, or set
   them as the `EBAY_APP_ID` / `EBAY_CERT_ID` environment variables (the
   script prefers env vars over the file, so you don't have to commit real
   credentials anywhere).

## Running it daily without your computer needing to be on

You picked "automated, runs on its own" — the way to get that without
renting a server is **GitHub Actions**, which is free for a script this
light. It's already set up in this project:

1. Create a new GitHub repo and push this folder to it. Use a **public**
   repo if you want the tracker app's Refresh Prices button to work via
   `raw.githubusercontent.com` (see below) — a private repo's raw URLs
   require auth the browser can't provide. Nothing sensitive lives in this
   repo as long as you keep API keys out of `config.json` and use secrets
   instead (next step), so public is reasonable here.
2. In the repo, go to **Settings → Secrets and variables → Actions** and
   add `EBAY_APP_ID` and `EBAY_CERT_ID` as secrets (skip if you're not
   using eBay).
3. That's it — `.github/workflows/daily-scrape.yml` runs the scraper every
   day at 13:00 UTC and commits the new CSV/JSON back into the repo under
   `output/`. Adjust the `cron:` line in that file to change the time
   (cron time is always UTC).
4. You can also trigger a run immediately from the repo's **Actions** tab
   → "Daily price scrape" → **Run workflow**, which is the fastest way to
   confirm it's working before you wait for the schedule.

### Alternative: cron on your own machine (Mac/Linux)

If you'd rather run it locally instead of on GitHub:

```bash
crontab -e
```

Add a line (runs at 9am daily):

```
0 9 * * * cd /full/path/to/gundam-zoid-scraper && /usr/bin/python3 scraper.py >> output/run.log 2>&1
```

### Alternative: Task Scheduler on Windows

Task Scheduler → Create Basic Task → Daily → set the action to run
`python.exe` with argument `scraper.py` and "Start in" set to the project
folder.

## A note on Terms of Service

Every site here has its own ToS, and several restrict automated access in
some form. This script is written to be low-volume and polite (a few
seconds between requests, one run a day, a normal browser User-Agent) but
that doesn't make it compliant with every site's specific terms — that's
worth checking yourself for any site you're relying on long-term,
especially before scaling up frequency or request volume. Where a site
offers an official API for this purpose (eBay does), the script uses that
instead of scraping.

## Connecting the tracker app

The tracker app has two ways of using this scraper's output now that it's
a full-catalog crawl instead of specific kit names:

- **Browse All tab** — shows every listing from the latest run directly,
  filterable by Gundam/Zoid and by retailer-vs-marketplace, searchable,
  sortable by price. This needs no setup beyond pointing the app at your
  data (next step) — there's nothing to name or match ahead of time.
- **My Kits tab** — your existing curated watchlist with price history
  over time. Each kit can have an optional "Match term"; on refresh, the
  app pulls in any scraped listing whose **title contains** that term
  (case-insensitive), rather than requiring an exact match — so a kit
  named "RX-78-2" with match term "rx-78-2" will pick up "MG 1/100
  RX-78-2 Gundam Ver. 2.0", "HGUC RX-78-2 Gundam Revive", etc. from
  across every site.

To wire either of these up: **publish `latest_full.json` somewhere
public.** The app fetches this file directly from your browser, so it
needs a URL that's reachable over the open internet and allows
cross-origin requests. The easiest option if you're using the GitHub
Actions setup below: use a **public** repo (private repos require auth
the browser can't provide) and point the app at the raw file, e.g.:
```
https://raw.githubusercontent.com/YOUR_USERNAME/YOUR_REPO/main/output/latest_full.json
```
`raw.githubusercontent.com` allows cross-origin fetches, so this works
without any extra server. If you'd rather keep the repo private, host
`latest_full.json` somewhere else public instead (e.g. a public S3
bucket, GitHub Pages, or any static host with CORS enabled) — a private
GitHub repo's raw URL will fail with a 404/401 from the browser.

Paste that URL into the app's ⚙ settings and save. From then on it
fetches fresh data automatically every time you open the app, and you can
also force a re-pull with the Refresh Prices button.

If you're not using GitHub Actions and running this locally/on cron
instead, you'll need some other way to make `output/latest_full.json`
reachable by URL — otherwise just open the file yourself after each run
and add anything interesting to My Kits by hand.

## Shipping cost

`latest_full.json` now includes a `shipping` field per result. eBay's
Browse API sometimes provides it (only when shipping is a fixed cost, not
"calculated" — those come through as `null`). The HTML-scraped sites don't
expose shipping on their search-results pages at all, so those always come
through as `null`. The tracker app has an "Include shipping" toggle and
lets you fill in a shipping cost by hand on any listing (via the "+ship"
link in a kit's history) when the scraper couldn't get one automatically.

## Keeping results to actual model kits

A plain "gundam"/"zoids" text search turns up more than kits — trading
cards, plush, keychains, apparel, and similar merch use the same words in
their titles. Two things narrow this down:

- **eBay** is restricted to eBay's own "Models & Kits" category
  (`category_ids=1188` in the Browse API call) rather than a plain
  keyword search, so non-kit listings are excluded at the source. I
  confirmed 1188 against eBay's own category browse URLs, but if it
  ever seems wrong, override it in `config.json`:
  ```json
  "ebay": { "category_id": "1234", ... }
  ```
- **Every other site** goes through a keyword blacklist
  (`NON_KIT_KEYWORDS` near the top of `scraper.py`) that drops any result
  whose title contains things like "trading card," "keychain," "plush,"
  "poster," "t-shirt," etc. — there's no category API to lean on for
  these sites, so this is a best-effort text filter, not a guarantee.
  If a real kit gets wrongly filtered (a title happens to contain one of
  the blacklisted words) or junk gets through, edit that list directly —
  it's a plain Python list, no other code needs to change.

## Product images

`latest_full.json` now includes an `image_url` per result where available
— eBay provides one directly via its API; HTML-scraped sites get theirs
from the product tile's `<img>` tag (each site's `image_selector` in
`SITE_DEFS`, defaulting to a plain `img` if not overridden). Both the
Browse All list and any kit you've Tracked into My Kits show this as a
thumbnail. If a site's images don't show up, its `image_selector` likely
needs the same kind of fix as title/price selectors — same process:
inspect a real product tile, find the actual `<img>`, check whether the
real URL lives in `src` or `data-src`.

## Kotobukiya

Corrected against real product HTML — turns out it's not a Shopify Dawn
theme like most of the others, it uses a custom `<product-card>` web
component, and the real domain is `kotobukiya-us.com` (the earlier guess,
`kotobukiya-shop.com`, was wrong). Its `SITE_DEFS` entry fully overrides
the shared defaults rather than relying on them.
