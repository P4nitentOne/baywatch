"""Pure functions: URL handling, label matching and value clean-up. No browser, easy to test."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import phonenumbers

ITEM_ID = re.compile(r"/itm/(?:[^/?#]+/)?(\d{9,})")
STORE_SLUG = re.compile(r"^/str/([^/?#]+)")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
VAT = re.compile(r"\b([A-Z]{2})\s*([0-9A-Z]{8,13})\b")


def item_id(url: str) -> str | None:
    m = ITEM_ID.search(url)
    return m.group(1) if m else None


def item_urls(hrefs: list[str], site: str) -> dict[str, str]:
    """Deduplicate listing links into {item_id: clean item url}, keeping page order."""
    out: dict[str, str] = {}
    for href in hrefs:
        iid = item_id(href)
        if iid and iid not in out:
            out[iid] = f"{site}/itm/{iid}"
    return out


def store_url(hrefs: list[str], site: str) -> str | None:
    """First /str/<slug> link on an item page, as a canonical store URL."""
    for href in hrefs:
        m = STORE_SLUG.match(urlsplit(href).path)
        if m:
            return f"{site}/str/{m.group(1).lower()}"
    return None


def canonical_store(url: str, site: str) -> str | None:
    """Normalise any store URL (tracking params, case) so old and new links deduplicate."""
    return store_url([url], site)


CATEGORY = re.compile(r"^/b/[^/]+/(\d+)(?:/|$)")


def category(href: str, site: str) -> tuple[str, str] | None:
    """(category id, canonical url) for a /b/<name>/<id>/bn_<n> link, else None."""
    path = urlsplit(href).path
    m = CATEGORY.match(path)
    if not m:
        return None
    return m.group(1), f"{site}{path.rstrip('/')}"


def child_categories(links: list[tuple[str, str]], site: str, own_id: str) -> list[tuple[str, str, str]]:
    """[(id, url, name)] from (href, text) pairs, deduplicated, without the page's own category.

    Brand/colour filters keep the parent's id (/b/Ricambi-Acerbis-per-moto/10063), so
    dropping our own id also drops them. Links back to the parent and to siblings stay;
    they are already known and the caller's INSERT OR IGNORE skips them.
    """
    out: dict[str, tuple[str, str, str]] = {}
    for href, text in links:
        cat = category(href, site)
        if cat and cat[0] != own_id and cat[0] not in out:
            name = text.replace("\xa0", " ").strip().split("\n")[0].strip()
            out[cat[0]] = (cat[0], cat[1], name)
    return list(out.values())


def listing_url(category_url: str, params: str) -> str:
    """Listing URL template with a {page} placeholder."""
    sep = "&" if params else ""
    return f"{category_url}?{params}{sep}_pgn={{page}}"


def root_matches(name: str, cat_id: str, roots: tuple[str, ...]) -> bool:
    if not roots:
        return True
    return any(r == cat_id or r.lower() in name.lower() for r in roots)


def match_fields(texts: list[str], labels: dict[str, tuple[str, ...]]) -> dict[str, str]:
    """Pair label/value texts from the About tab's business details block.

    The block is a flat run of spans: label, value, label, value... A value can be
    empty, and a label can be missing, so this walks the list and only consumes a
    value when the text before it matched a known label.
    """
    clean = [t.replace("\xa0", " ").strip() for t in texts]
    out: dict[str, str] = {}
    i = 0
    while i < len(clean) - 1:
        field = _label_field(clean[i], labels)
        if field and field not in out:
            value = clean[i + 1]
            if value and not _label_field(value, labels):
                out[field] = value
                i += 2
                continue
        i += 1
    return out


def _label_field(text: str, labels: dict[str, tuple[str, ...]]) -> str | None:
    key = text.rstrip(":").strip().lower()
    if not key or len(key) > 40:
        return None
    # Exact matches win over substring matches, so "nome" doesn't claim "cognome".
    for field, names in labels.items():
        if key in names:
            return field
    for field, names in labels.items():
        if any(len(n) > 4 and n in key for n in names):
            return field
    return None


def location(texts: list[str]) -> str | None:
    """'Luogo: Italia' / 'Location: Italy' from the About tab description."""
    for t in texts:
        t = t.replace("\xa0", " ").strip()
        head, sep, tail = t.partition(":")
        if sep and head.strip().lower() in ("luogo", "location", "standort", "lieu", "ubicación"):
            return tail.strip() or None
    return None


def normalize(fields: dict[str, str], phone_region: str) -> dict[str, str]:
    out = dict(fields)
    for key in ("phone", "fax"):
        if out.get(key):
            out[key] = phone(out[key], phone_region)
    if out.get("vat"):
        out["vat"] = vat(out["vat"], phone_region)
    if out.get("email") and not EMAIL.match(out["email"]):
        out["further_info"] = "; ".join(filter(None, [out.get("further_info"), f"email as shown: {out['email']}"]))
        del out["email"]
    return out


def phone(raw: str, region: str) -> str:
    """E.164 (+390612345678) when parseable, otherwise the original text."""
    try:
        num = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        return raw
    if not phonenumbers.is_possible_number(num):
        return raw
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


def vat(raw: str, country: str) -> str:
    """Pick the VAT number for `country` out of a list like 'IT 0243..., DE 1234...'.

    Falls back to the first VAT number found, then to the raw text.
    """
    found = [(cc, num) for cc, num in VAT.findall(raw.upper())]
    for cc, num in found:
        if cc == country.upper():
            return cc + num
    if found:
        return found[0][0] + found[0][1]
    return raw.strip()
