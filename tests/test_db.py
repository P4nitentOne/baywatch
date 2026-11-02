from pathlib import Path

from baywatch.config import Category, Config, Discovery, Pacing
from baywatch.db import Store


def make_cfg(tmp_path: Path) -> Config:
    return Config(
        site="https://www.ebay.it",
        country="Italia",
        phone_region="IT",
        database=tmp_path / "t.db",
        profile_dir=tmp_path / "profile",
        browser_channel=None,
        workers=2,
        block_images=True,
        captcha_solver="manual",
        seller_location="Italia",
        pacing=Pacing(),
        discovery=Discovery(),
        categories=(Category(id="a", url="https://www.ebay.it/b/x?_pgn={page}", max_pages=5),),
    )


def test_seed_is_idempotent(tmp_path):
    cfg = make_cfg(tmp_path)
    store = Store(cfg.database)
    assert store.seed_pages(cfg) == 5
    assert store.seed_pages(cfg) == 0


def test_flow_and_dedupe(tmp_path):
    cfg = make_cfg(tmp_path)
    store = Store(cfg.database)
    store.seed_pages(cfg)

    page = store.claim_page()
    assert page["page"] == 1
    assert store.page_done(page["url"], "a", {"1": "u1", "2": "u2"}) == 2
    assert store.page_done(page["url"], "a", {"1": "u1"}) == 0

    assert store.item_done("1", "https://www.ebay.it/str/shop") is True
    assert store.item_done("2", "https://www.ebay.it/str/shop") is False  # same seller, not queued twice

    seller = store.claim_seller()
    assert seller["store_url"] == "https://www.ebay.it/str/shop"
    store.seller_done(seller["store_url"], "done", "Italia", {"company": "ACME"})
    assert store.claim_seller() is None
    assert [r["company"] for r in store.sellers_for_export(include_all=False)] == ["ACME"]


def test_fail_retries_then_gives_up(tmp_path):
    cfg = make_cfg(tmp_path)
    store = Store(cfg.database)
    store.seed_pages(cfg)
    url = store.claim_page()["url"]
    store.fail("pages", url, "blocked", max_attempts=2)
    assert store.claim_page()["url"] == url
    store.fail("pages", url, "blocked", max_attempts=2)
    assert store.claim_page()["url"] != url
    assert store.requeue_failed() == 1


def test_category_end_closes_later_pages(tmp_path):
    cfg = make_cfg(tmp_path)
    store = Store(cfg.database)
    store.seed_pages(cfg)
    store.category_ended("https://www.ebay.it/b/x?_pgn=3", "a", 3)
    assert store.counts()["pages"] == {"pending": 2, "end": 3}


def test_import_skips_known_sellers(tmp_path):
    cfg = make_cfg(tmp_path)
    store = Store(cfg.database)
    assert store.import_seller("https://www.ebay.it/str/old", "Italia", {"company": "Old"}) is True
    assert store.import_seller("https://www.ebay.it/str/old", "Italia", {"company": "Old"}) is False
    assert store.item_done("9", "https://www.ebay.it/str/old") is False
    assert store.claim_seller() is None


def test_claims_are_exclusive_and_released(tmp_path):
    cfg = make_cfg(tmp_path)
    store = Store(cfg.database)
    store.seed_pages(cfg)
    a, b = store.claim_page(), store.claim_page()
    assert a["url"] != b["url"]
    assert store.busy()
    store.release_claims()
    assert not store.busy()
    assert store.counts()["pages"] == {"pending": 5}


def test_category_tree_and_leaf_pages(tmp_path):
    cfg = make_cfg(tmp_path)
    store = Store(cfg.database)
    store.seed_discovery(cfg)
    root = store.claim_category()
    assert root["cat_id"] == "root" and root["depth"] == -1

    store.add_categories([
        {"cat_id": "10", "url": "u10", "name": "Top", "parent": "root", "root": "10", "depth": 0, "status": "done", "leaf": 0},
        {"cat_id": "11", "url": "u11", "name": "Sub", "parent": "10", "root": "10", "depth": 1},
    ])
    store.category_done("root", leaf=False, listing_url=None, max_pages=0)

    sub = store.claim_category()
    assert sub["cat_id"] == "11"
    assert store.add_categories([{"cat_id": "12", "url": "u12", "name": "Leaf", "parent": "11", "root": "10", "depth": 2}]) == 1
    store.category_done("11", leaf=False, listing_url=None, max_pages=0)

    leaf = store.claim_category()
    assert leaf["cat_id"] == "12"  # deepest first
    store.category_done("12", leaf=True, listing_url="u12?_pgn={page}", max_pages=3)
    assert store.claim_page()["url"] == "u12?_pgn=1"
    assert store.claim_category() is None

    [summary] = store.category_summary()
    assert (summary["name"], summary["found"], summary["leaves"], summary["pages_total"]) == ("Top", 3, 1, 3)
