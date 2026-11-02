"""SQLite state: every category, listing page, item and seller, with its status. This is the whole job queue."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from baywatch.config import FIELDS, Config

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS categories (
    cat_id     TEXT PRIMARY KEY,                 -- eBay category id, or 'root' for the all-categories page
    url        TEXT NOT NULL,
    name       TEXT,
    parent     TEXT,
    root       TEXT,                             -- top-level category id, for grouping
    depth      INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending | working | done | failed
    leaf       INTEGER,                          -- 1 = listings scraped here, 0 = has subcategories
    attempts   INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS pages (
    url        TEXT PRIMARY KEY,
    category   TEXT NOT NULL,
    page       INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending | working | done | end | failed
    attempts   INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS items (
    item_id    TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    category   TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending | done | no_store | failed
    attempts   INTEGER NOT NULL DEFAULT 0,
    store_url  TEXT,
    error      TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS sellers (
    store_url    TEXT PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | done | no_info | wrong_location | failed
    attempts     INTEGER NOT NULL DEFAULT 0,
    location     TEXT,
    {", ".join(f"{f} TEXT" for f in FIELDS)},
    source_item  TEXT,
    error        TEXT,
    discovered_at TEXT,
    scraped_at   TEXT
);
CREATE INDEX IF NOT EXISTS categories_status ON categories(status, depth);
CREATE INDEX IF NOT EXISTS pages_status ON pages(status, category, page);
CREATE INDEX IF NOT EXISTS items_status ON items(status);
CREATE INDEX IF NOT EXISTS sellers_status ON sellers(status);
"""

TABLES = ("categories", "pages", "items", "sellers")
KEYS = {"categories": "cat_id", "pages": "url", "items": "item_id", "sellers": "store_url"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def seed_pages(self, cfg: Config) -> int:
        rows = [
            (c.url.format(page=p), c.id, p)
            for c in cfg.categories
            for p in range(1, c.max_pages + 1)
        ]
        with self.conn:
            cur = self.conn.executemany(
                "INSERT OR IGNORE INTO pages(url, category, page) VALUES (?, ?, ?)", rows
            )
        return cur.rowcount

    def seed_discovery(self, cfg: Config) -> None:
        if cfg.discovery.enabled:
            with self.conn:
                self.conn.execute(
                    "INSERT OR IGNORE INTO categories(cat_id, url, name, depth) VALUES ('root', ?, 'all categories', -1)",
                    (f"{cfg.site}/n/all-categories",),
                )

    def add_categories(self, rows: list[dict]) -> int:
        """Insert newly found categories. Returns how many were not known yet."""
        with self.conn:
            cur = self.conn.executemany(
                """INSERT OR IGNORE INTO categories(cat_id, url, name, parent, root, depth, status, leaf)
                   VALUES (:cat_id, :url, :name, :parent, :root, :depth, :status, :leaf)""",
                [{"status": "pending", "leaf": None, **r} for r in rows],
            )
        return cur.rowcount

    def category_done(self, cat_id: str, leaf: bool, listing_url: str | None, max_pages: int) -> None:
        """Mark a category explored. A leaf gets its listing pages queued (listing_url has {page})."""
        with self.conn:
            self.conn.execute(
                "UPDATE categories SET status = 'done', leaf = ?, error = NULL, updated_at = ? WHERE cat_id = ?",
                (int(leaf), now(), cat_id),
            )
            if leaf and listing_url:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO pages(url, category, page) VALUES (?, ?, ?)",
                    [(listing_url.format(page=p), cat_id, p) for p in range(1, max_pages + 1)],
                )

    def category_summary(self) -> list[sqlite3.Row]:
        """Per top-level category: subcategories found, leaves, and listing pages done/total."""
        return self.conn.execute(
            """SELECT top.name AS name, top.cat_id AS cat_id,
                      COUNT(c.cat_id) AS found,
                      SUM(c.status = 'pending') AS unexplored,
                      SUM(c.leaf = 1) AS leaves,
                      (SELECT COUNT(*) FROM pages p JOIN categories l ON l.cat_id = p.category
                        WHERE l.root = top.cat_id AND p.status IN ('done', 'end')) AS pages_done,
                      (SELECT COUNT(*) FROM pages p JOIN categories l ON l.cat_id = p.category
                        WHERE l.root = top.cat_id) AS pages_total
               FROM categories top LEFT JOIN categories c ON c.root = top.cat_id
               WHERE top.depth = 0
               GROUP BY top.cat_id ORDER BY top.name"""
        ).fetchall()

    # ---- claiming work ------------------------------------------------------
    # A claimed row is 'working' so parallel tabs never pick the same one.
    # Rows left 'working' by a killed run are released on the next start.

    def release_claims(self) -> None:
        with self.conn:
            for table in TABLES:
                self.conn.execute(f"UPDATE {table} SET status = 'pending' WHERE status = 'working'")

    def _claim(self, table: str, order: str) -> sqlite3.Row | None:
        with self.conn:
            return self.conn.execute(
                f"""UPDATE {table} SET status = 'working'
                    WHERE rowid = (SELECT rowid FROM {table} WHERE status = 'pending' ORDER BY {order} LIMIT 1)
                    RETURNING *"""
            ).fetchone()

    def claim_seller(self) -> sqlite3.Row | None:
        return self._claim("sellers", "discovered_at")

    def claim_item(self) -> sqlite3.Row | None:
        return self._claim("items", "rowid")

    def claim_page(self) -> sqlite3.Row | None:
        return self._claim("pages", "page, category")

    def claim_category(self) -> sqlite3.Row | None:
        # Deepest first: reaches leaves (and therefore listings) quickly instead of
        # exploring every top-level branch before the first item is found.
        return self._claim("categories", "depth DESC, rowid")

    def busy(self) -> bool:
        return any(
            self.conn.execute(f"SELECT 1 FROM {t} WHERE status = 'working' LIMIT 1").fetchone() for t in TABLES
        )

    # ---- recording results --------------------------------------------------

    def fail(self, table: str, key: str, error: str, max_attempts: int) -> None:
        """Count a failed attempt. The row stays pending until it runs out of attempts."""
        col = KEYS[table]
        stamp = "scraped_at" if table == "sellers" else "updated_at"
        with self.conn:
            self.conn.execute(
                f"""UPDATE {table}
                    SET attempts = attempts + 1, error = ?, {stamp} = ?,
                        status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE 'pending' END
                    WHERE {col} = ?""",
                (error, now(), max_attempts, key),
            )

    def page_done(self, url: str, category: str, item_urls: dict[str, str]) -> int:
        """Store the items found on a listing page. Returns how many were new."""
        with self.conn:
            self.conn.execute(
                "UPDATE pages SET status = 'done', error = NULL, updated_at = ? WHERE url = ?",
                (now(), url),
            )
            cur = self.conn.executemany(
                "INSERT OR IGNORE INTO items(item_id, url, category) VALUES (?, ?, ?)",
                [(iid, iurl, category) for iid, iurl in item_urls.items()],
            )
        return cur.rowcount

    def category_ended(self, url: str, category: str, page: int) -> None:
        """The category has no more (new) results: close this page and every later one."""
        with self.conn:
            self.conn.execute(
                """UPDATE pages SET status = 'end', updated_at = ?
                   WHERE category = ? AND page >= ? AND status IN ('pending', 'working')""",
                (now(), category, page),
            )

    def item_done(self, item_id: str, store_url: str | None) -> bool:
        """Record which store an item belongs to. Returns True if the seller is new."""
        with self.conn:
            self.conn.execute(
                "UPDATE items SET status = ?, store_url = ?, error = NULL, updated_at = ? WHERE item_id = ?",
                ("done" if store_url else "no_store", store_url, now(), item_id),
            )
            if not store_url:
                return False
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO sellers(store_url, source_item, discovered_at) VALUES (?, ?, ?)",
                (store_url, item_id, now()),
            )
        return cur.rowcount == 1

    def seller_done(self, store_url: str, status: str, location: str | None, fields: dict[str, str]) -> None:
        cols = ", ".join(f"{f} = :{f}" for f in FIELDS)
        params = {f: fields.get(f) for f in FIELDS}
        params.update(store_url=store_url, status=status, location=location, ts=now())
        with self.conn:
            self.conn.execute(
                f"""UPDATE sellers SET status = :status, location = :location, {cols},
                        error = NULL, scraped_at = :ts
                    WHERE store_url = :store_url""",
                params,
            )

    def import_seller(self, store_url: str, location: str | None, fields: dict[str, str]) -> bool:
        """Add an already-scraped seller (e.g. from an old CSV) so it is never visited again."""
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO sellers(store_url, discovered_at) VALUES (?, ?)",
                (store_url, now()),
            )
        if cur.rowcount:
            self.seller_done(store_url, "done", location, fields)
        return cur.rowcount == 1

    # ---- maintenance --------------------------------------------------------

    def requeue_failed(self) -> int:
        total = 0
        with self.conn:
            for table in TABLES:
                total += self.conn.execute(
                    f"UPDATE {table} SET status = 'pending', attempts = 0 WHERE status = 'failed'"
                ).rowcount
        return total

    def counts(self) -> dict[str, dict[str, int]]:
        return {
            table: dict(self.conn.execute(f"SELECT status, COUNT(*) FROM {table} GROUP BY status").fetchall())
            for table in TABLES
        }

    def sellers_for_export(self, include_all: bool) -> list[sqlite3.Row]:
        where = "" if include_all else "WHERE status = 'done'"
        return self.conn.execute(f"SELECT * FROM sellers {where} ORDER BY scraped_at").fetchall()
