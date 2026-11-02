from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

FIELDS = ("company", "first_name", "last_name", "address", "phone", "fax", "email", "vat", "further_info")


@dataclass(frozen=True)
class Category:
    id: str
    url: str
    max_pages: int = 100


@dataclass(frozen=True)
class Discovery:
    """Find categories automatically, starting from the site's "all categories" page."""

    enabled: bool = True
    # Top-level categories to crawl, by name (substring, case-insensitive) or category id.
    # Empty = all of them.
    roots: tuple[str, ...] = ()
    # How deep to follow subcategories. Item listings are scraped only at the leaves.
    max_depth: int = 4
    # Listing pages per leaf category. eBay stops paginating around 10 pages.
    max_pages: int = 10
    # Appended to every listing URL. LH_PrefLoc=1 = items located in the site's country.
    listing_params: str = "LH_PrefLoc=1&mag=1"


@dataclass(frozen=True)
class Pacing:
    min_delay: float = 1.0
    max_delay: float = 3.0
    ready_timeout: float = 6.0
    error_refreshes: int = 4
    free_retries: int = 2
    backoff_base: float = 10.0
    backoff_max: float = 120.0
    break_every: int = 0
    break_seconds: float = 30.0
    captcha_timeout: float = 600.0
    max_attempts: int = 4


@dataclass(frozen=True)
class Config:
    site: str
    country: str
    phone_region: str
    database: Path
    profile_dir: Path
    browser_channel: str | None
    workers: int
    block_images: bool
    # "clip" runs the automatic hCaptcha solver first; "manual" only waits for you to solve it.
    captcha_solver: str
    # Skip sellers whose About tab shows a different location. Empty string disables the filter.
    seller_location: str
    pacing: Pacing
    discovery: Discovery
    # Fixed listing URLs from the config. Used alongside (or instead of) discovery.
    categories: tuple[Category, ...]
    labels: dict[str, tuple[str, ...]] = field(default_factory=dict)


def load(path: str | Path) -> Config:
    path = Path(path)
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    base = path.parent

    labels = {name: tuple(s.lower() for s in raw.get("labels", {}).get(name, ())) for name in FIELDS}
    categories = tuple(Category(**c) for c in raw.get("categories", ()))
    disc = dict(raw.get("discovery", {}))
    disc["roots"] = tuple(str(r) for r in disc.get("roots", ()))
    discovery = Discovery(**disc)
    if not categories and not discovery.enabled:
        raise ValueError(f"{path}: discovery is disabled and no [[categories]] are defined")
    for c in categories:
        if "{page}" not in c.url:
            raise ValueError(f"{path}: category {c.id!r} url has no {{page}} placeholder")

    solver = raw.get("captcha_solver", "manual")
    if solver not in ("manual", "clip"):
        raise ValueError(f"{path}: captcha_solver must be 'manual' or 'clip', got {solver!r}")

    return Config(
        site=raw["site"].rstrip("/"),
        country=raw.get("country", ""),
        phone_region=raw.get("phone_region", "IT"),
        database=base / raw.get("database", "baywatch.db"),
        profile_dir=base / raw.get("profile_dir", ".profile"),
        browser_channel=raw.get("browser_channel") or None,
        workers=max(1, int(raw.get("workers", 4))),
        block_images=bool(raw.get("block_images", True)),
        captcha_solver=solver,
        seller_location=raw.get("seller_location", ""),
        pacing=Pacing(**raw.get("pacing", {})),
        discovery=discovery,
        categories=categories,
        labels=labels,
    )
