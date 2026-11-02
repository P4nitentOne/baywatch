"""The stages, deepest first:

    categories -> leaf categories -> listing pages -> items -> seller stores -> business details

`workers` browsers run the same loop in parallel, each claiming the deepest pending
work first, so finished sellers show up from the first minute and new categories
are only explored when the current ones run dry.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

from baywatch import parse
from baywatch.browser import Browser, Outcome, Tab
from baywatch.config import Config
from baywatch.db import Store

log = logging.getLogger(__name__)

# Listing layouts differ per category. Grid cards first, so recommendation carousels are ignored.
CARD_SELECTORS = ("li.brwrvr__item-card", "li.su-grid__item", "li.s-item", "li.s-card")
ITEM_LINK = "a[href*='/itm/']"
STORE_LINK = "a[href*='/str/']"
SKELETON = ".brw-loaders__skeleton"
ABOUT_LOCATION = "section.str-about-description__seller-info span"
ABOUT_BUSINESS = "div.str-business-details__seller-info"
# Subcategory navigation on a category page, for both listing layouts.
CHILD_LINKS = ".su-subcategory-list a[href*='/b/'], .brwel__items a[href*='/b/']"
BREADCRUMBS = ".breadcrumbs a[href*='/b/']"
# /n/all-categories: each top-level link followed by its list of subcategories, in document order.
ALL_CATEGORIES = "a.cat-url, ul.sub-cats a"
ALL_CATEGORIES_JS = "els => els.map(e => [e.matches('a.cat-url') ? 'top' : 'sub', e.href, e.innerText])"


class Run:
    def __init__(self, cfg: Config, store: Store, browser: Browser, max_sellers: int | None):
        self.cfg = cfg
        self.store = store
        self.browser = browser
        self.max_sellers = max_sellers
        self.sellers_done = 0
        self.started = time.monotonic()

    @property
    def target_reached(self) -> bool:
        return self.max_sellers is not None and self.sellers_done >= self.max_sellers

    async def worker(self, n: int) -> None:
        tab = await self.browser.new_tab(n)
        while not self.target_reached:
            if row := self.store.claim_seller():
                step, table, key = self.scrape_seller, "sellers", row["store_url"]
            elif row := self.store.claim_item():
                step, table, key = self.scrape_item, "items", row["item_id"]
            elif row := self.store.claim_page():
                step, table, key = self.scrape_page, "pages", row["url"]
            elif row := self.store.claim_category():
                step, table, key = self.scrape_category, "categories", row["cat_id"]
            elif self.store.busy():
                await asyncio.sleep(1)  # other tabs may still produce work
                continue
            else:
                return
            try:
                await step(tab, row)
            except Exception as e:
                # One odd page must not take the other tabs down with it.
                log.warning("[tab %d] %s %s: %s", n, table, key, str(e).splitlines()[0])
                self.store.fail(table, key, f"error: {e}"[:300], self.cfg.pacing.max_attempts)

    async def report(self) -> None:
        while True:
            await asyncio.sleep(60)
            mins = (time.monotonic() - self.started) / 60
            log.info("== %d sellers in %.0f min (%.0f/hour)", self.sellers_done, mins, self.sellers_done / mins * 60)

    # ---- stages -------------------------------------------------------------

    async def scrape_category(self, tab: Tab, row: sqlite3.Row) -> None:
        if row["depth"] < 0:
            await self.scrape_all_categories(tab, row)
            return

        cat_id, name, depth = row["cat_id"], row["name"], row["depth"]
        ready = ", ".join([CHILD_LINKS, *(f"{sel} {ITEM_LINK}" for sel in CARD_SELECTORS)])
        outcome = await tab.open(row["url"], ready=ready)
        if outcome is not Outcome.OK:
            self.store.fail("categories", cat_id, outcome.value, self.cfg.pacing.max_attempts)
            return

        new = 0
        if depth < self.cfg.discovery.max_depth:
            # The nav lists children on a parent page, and parent + siblings on a leaf.
            # Siblings and the parent are already known, so a leaf adds nothing new.
            # Breadcrumbs are ancestors, never children, even if we haven't seen them yet.
            crumbs = {c[0] for h, _ in await tab.links(BREADCRUMBS) if (c := parse.category(h, self.cfg.site))}
            children = [
                c for c in parse.child_categories(await tab.links(CHILD_LINKS), self.cfg.site, cat_id)
                if c[0] not in crumbs
            ]
            new = self.store.add_categories([
                {"cat_id": cid, "url": url, "name": cname, "parent": cat_id, "root": row["root"], "depth": depth + 1}
                for cid, url, cname in children
            ])

        leaf = new == 0
        listing = parse.listing_url(row["url"], self.cfg.discovery.listing_params) if leaf else None
        self.store.category_done(cat_id, leaf, listing, self.cfg.discovery.max_pages)
        if leaf:
            log.info("[cat] %s (depth %d): leaf, %d listing pages queued", name, depth, self.cfg.discovery.max_pages)
        else:
            log.info("[cat] %s (depth %d): %d new subcategories", name, depth, new)

    async def scrape_all_categories(self, tab: Tab, row: sqlite3.Row) -> None:
        outcome = await tab.open(row["url"], ready=ALL_CATEGORIES)
        if outcome is not Outcome.OK:
            self.store.fail("categories", row["cat_id"], outcome.value, self.cfg.pacing.max_attempts)
            return

        roots = self.cfg.discovery.roots
        rows, top, keep = [], None, False
        for kind, href, text in await tab.read(ALL_CATEGORIES, ALL_CATEGORIES_JS):
            cat = parse.category(href, self.cfg.site)
            if not cat:
                continue
            name = text.replace("\xa0", " ").strip().split("\n")[0].strip()
            if kind == "top":
                top, keep = cat[0], parse.root_matches(name, cat[0], roots)
                if keep:
                    # Top-level pages are hubs without pagination; only their subcategories are crawled.
                    rows.append({"cat_id": top, "url": cat[1], "name": name, "parent": "root", "root": top,
                                 "depth": 0, "status": "done", "leaf": 0})
            elif top and keep:
                rows.append({"cat_id": cat[0], "url": cat[1], "name": name, "parent": top, "root": top, "depth": 1})

        if not rows:
            self.store.fail("categories", row["cat_id"], "no categories found (layout changed?)", self.cfg.pacing.max_attempts)
            if roots:
                log.warning("[cat] no top-level category matched roots %s", list(roots))
            return
        self.store.add_categories(rows)
        self.store.category_done(row["cat_id"], leaf=False, listing_url=None, max_pages=0)
        tops = sum(r["depth"] == 0 for r in rows)
        log.info("[cat] all categories: %d top-level, %d subcategories to explore", tops, len(rows) - tops)

    async def scrape_page(self, tab: Tab, row: sqlite3.Row) -> None:
        url, cat, page = row["url"], row["category"], row["page"]
        ready = ", ".join(f"{sel} {ITEM_LINK}" for sel in CARD_SELECTORS)
        outcome = await tab.open(url, ready=ready)
        if outcome is not Outcome.OK:
            self.store.fail("pages", url, outcome.value, self.cfg.pacing.max_attempts)
            return

        hrefs: list[str] = []
        for sel in CARD_SELECTORS:
            hrefs = await tab.hrefs(f"{sel} {ITEM_LINK}")
            if hrefs:
                break
        else:
            hrefs = await tab.hrefs(ITEM_LINK)
        items = parse.item_urls(hrefs, self.cfg.site)

        if not items:
            # Skeleton loaders without results = eBay's soft "Si è verificato un errore" block.
            if await tab.count(SKELETON):
                tab.blocked()
                self.store.fail("pages", url, "listings failed to load (soft block)", self.cfg.pacing.max_attempts)
            else:
                log.info("[page] %s p%d: no items, category finished", cat, page)
                self.store.category_ended(url, cat, page)
            return

        new = self.store.page_done(url, cat, items)
        log.info("[page] %s p%d: %d items, %d new", cat, page, len(items), new)
        if new == 0 and page > 1:
            # eBay serves the last page again (or a hub page) when you go past the end.
            log.info("[page] %s: no new items on p%d, category finished", cat, page)
            self.store.category_ended(url, cat, page + 1)

    async def scrape_item(self, tab: Tab, row: sqlite3.Row) -> None:
        iid, url = row["item_id"], row["url"]
        outcome = await tab.open(url, ready=STORE_LINK)
        if outcome is not Outcome.OK:
            self.store.fail("items", iid, outcome.value, self.cfg.pacing.max_attempts)
            return
        shop = parse.store_url(await tab.hrefs(STORE_LINK), self.cfg.site)
        is_new = self.store.item_done(iid, shop)
        if shop:
            log.debug("[item] %s -> %s%s", iid, shop, " (new seller)" if is_new else "")
        else:
            log.debug("[item] %s: seller has no eBay store", iid)

    async def scrape_seller(self, tab: Tab, row: sqlite3.Row) -> None:
        shop = row["store_url"]
        outcome = await tab.open(f"{shop}?_tab=about", ready=ABOUT_BUSINESS)
        if outcome is not Outcome.OK:
            self.store.fail("sellers", shop, outcome.value, self.cfg.pacing.max_attempts)
            return

        loc = parse.location(await tab.texts(ABOUT_LOCATION))
        fields = parse.match_fields(await tab.texts(f"{ABOUT_BUSINESS} span"), self.cfg.labels)
        fields = parse.normalize(fields, self.cfg.phone_region)

        if self.cfg.seller_location and (loc or "").lower() != self.cfg.seller_location.lower():
            status = "wrong_location"
        elif not fields:
            status = "no_info"
        else:
            status = "done"
        self.store.seller_done(shop, status, loc, fields)

        if status == "done":
            self.sellers_done += 1
            log.info("[seller] %s: %s (%s)", shop, fields.get("company", "?"), ", ".join(fields))
        else:
            log.info("[seller] %s: %s (location: %s)", shop, status, loc)


async def run(cfg: Config, store: Store, browser: Browser, max_sellers: int | None = None) -> None:
    store.release_claims()
    store.seed_pages(cfg)
    store.seed_discovery(cfg)
    r = Run(cfg, store, browser, max_sellers)
    reporter = asyncio.create_task(r.report())
    try:
        await asyncio.gather(*(r.worker(i) for i in range(cfg.workers)))
    finally:
        reporter.cancel()
        store.release_claims()
    if r.target_reached:
        log.info("reached --max-sellers %d", max_sellers)
    else:
        log.info("nothing left to do")
    mins = max((time.monotonic() - r.started) / 60, 1e-9)
    log.info("done: %d sellers in %.1f min", r.sellers_done, mins)
