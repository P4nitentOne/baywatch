from __future__ import annotations

import argparse
import csv
import dataclasses
import logging
import sys
from pathlib import Path

from baywatch import config, parse
from baywatch.db import Store

log = logging.getLogger("baywatch")

# Same columns as scrapita's ebay_sellers.csv, plus status and timestamp.
COLUMNS = {
    "Location": "location",
    "Store Link": "store_url",
    "Company Name": "company",
    "First Name": "first_name",
    "Last Name": "last_name",
    "Address": "address",
    "Phone": "phone",
    "Fax": "fax",
    "Email": "email",
    "VAT": "vat",
    "Further Info": "further_info",
    "Status": "status",
    "Scraped At": "scraped_at",
}


def cmd_run(cfg: config.Config, store: Store, args: argparse.Namespace) -> None:
    import asyncio

    from playwright.async_api import async_playwright

    from baywatch import captcha, pipeline
    from baywatch.browser import Browser

    if args.workers:
        cfg = dataclasses.replace(cfg, workers=args.workers)
    if cfg.captcha_solver == "clip":
        try:
            captcha.warm_up()
        except captcha.SolverUnavailable as e:
            log.warning("%s; captchas will wait for you instead", e)

    async def go() -> None:
        async with async_playwright() as pw:
            browser = Browser(pw, cfg)
            try:
                await pipeline.run(cfg, store, browser, max_sellers=args.max_sellers)
            finally:
                await browser.close()

    asyncio.run(go())


def cmd_status(cfg: config.Config, store: Store, args: argparse.Namespace) -> None:
    for table, counts in store.counts().items():
        summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "empty"
        print(f"{table:8} {summary}")


def cmd_categories(cfg: config.Config, store: Store, args: argparse.Namespace) -> None:
    rows = store.category_summary()
    if not rows:
        print("no categories discovered yet; `baywatch run` starts with the all-categories page")
        return
    print(f"{'top-level category':42} {'id':>8} {'found':>6} {'to visit':>8} {'leaves':>7} {'pages done':>11}")
    for r in rows:
        pages = f"{r['pages_done']}/{r['pages_total']}"
        print(f"{r['name'][:42]:42} {r['cat_id']:>8} {r['found']:>6} {r['unexplored'] or 0:>8} {r['leaves'] or 0:>7} {pages:>11}")


def cmd_export(cfg: config.Config, store: Store, args: argparse.Namespace) -> None:
    rows = store.sellers_for_export(include_all=args.all)
    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for r in rows:
            w.writerow([r[col] if r[col] is not None else "n/a" for col in COLUMNS.values()])
    print(f"wrote {len(rows)} sellers to {args.output}")


def cmd_import(cfg: config.Config, store: Store, args: argparse.Namespace) -> None:
    """Load an old scrapita CSV so those sellers are skipped (and included in exports)."""
    reverse = {v: k for k, v in COLUMNS.items()}
    added = skipped = 0
    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            shop = parse.canonical_store(row.get("Store Link", ""), cfg.site)
            if not shop:
                skipped += 1
                continue
            fields = {
                field: value
                for field in config.FIELDS
                if (value := (row.get(reverse[field]) or "").strip()) and value != "n/a"
            }
            loc = (row.get("Location") or "").strip() or None
            if store.import_seller(shop, loc, parse.normalize(fields, cfg.phone_region)):
                added += 1
            else:
                skipped += 1
    print(f"imported {added} sellers, skipped {skipped} (duplicates or bad links)")


def cmd_retry(cfg: config.Config, store: Store, args: argparse.Namespace) -> None:
    print(f"requeued {store.requeue_failed()} failed rows")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="baywatch", description="Slow-motion eBay seller lead finder.")
    parser.add_argument("-c", "--config", default="config.toml", help="config file (default: config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="discover and scrape sellers (Ctrl+C to stop; resumes where it left off)")
    p.add_argument("--max-sellers", type=int, help="stop after this many new sellers with business info")
    p.add_argument("-w", "--workers", type=int, help="parallel tabs (overrides config)")
    p.set_defaults(func=cmd_run)

    sub.add_parser("status", help="show progress counts").set_defaults(func=cmd_status)
    sub.add_parser("categories", help="discovered categories per top-level category").set_defaults(func=cmd_categories)

    p = sub.add_parser("export", help="write sellers to CSV")
    p.add_argument("-o", "--output", default="sellers.csv")
    p.add_argument("--all", action="store_true", help="include sellers without business info / wrong location")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import-csv", help="import sellers from an old scrapita ebay_sellers.csv")
    p.add_argument("csv", type=Path)
    p.set_defaults(func=cmd_import)

    sub.add_parser("retry-failed", help="put failed pages/items/sellers back in the queue").set_defaults(func=cmd_retry)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpx2", "huggingface_hub", "transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        sys.exit(f"{cfg_path} not found. Copy config.example.toml to config.toml and edit it.")
    cfg = config.load(cfg_path)
    store = Store(cfg.database)
    try:
        args.func(cfg, store, args)
    except KeyboardInterrupt:
        log.info("stopped. Progress is saved; run again to continue.")
    finally:
        store.release_claims()
        store.close()
