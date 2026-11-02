"""Automatic hCaptcha solver, ported from scrapita's playwright_consumer.py.

Clicks the checkbox; if an image challenge appears, classifies the tiles with CLIP
and clicks the matches. The heavy imports (torch, transformers) are loaded on first
use, so the rest of baywatch runs without them when captcha_solver = "manual".
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from functools import lru_cache
from pathlib import Path

from playwright.async_api import Frame, Page

log = logging.getLogger(__name__)

IFRAME = "iframe[src*='hcaptcha.com/captcha/v1']"
KEYBOARD_REF = Path(__file__).parent / "assets" / "keyboard_ref.jpg"
SIMILARITY_THRESHOLD = 0.85
MAX_ROUNDS = 3

LABEL_CONFIGS = {
    "bird_shelter": {
        "tile_labels": ["bird", "cat", "dog", "mammal", "something else"],
        "target_label": "bird",
        "threshold": 0.35,
    },
    "mouse_keyboard": {
        "tile_labels": ["computer mouse", "keyboard", "computer equipment", "electronics", "something else"],
        "target_label": "computer mouse",
        "threshold": 0.35,
    },
}


class SolverUnavailable(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _clip():
    try:
        import torch
        from transformers import CLIPModel, CLIPProcessor
    except ImportError as e:
        raise SolverUnavailable(
            "captcha_solver = 'clip' needs the solver extras: pip install -e .[solver]"
        ) from e
    device = "cuda" if torch.cuda.is_available() else "cpu"
    log.info("loading CLIP model on %s (first time downloads ~600 MB)", device)
    model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32").to(device)
    processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
    return torch, model, processor, device


def warm_up() -> None:
    """Load the model up front, so the first captcha isn't also a 600 MB download."""
    _clip()


def _classify(img_path: Path, labels: list[str]) -> tuple[str, float]:
    from PIL import Image

    torch, model, processor, device = _clip()
    img = Image.open(img_path).convert("RGB")
    inputs = processor(text=labels, images=img, return_tensors="pt", padding=True).to(device)
    with torch.no_grad():
        probs = model(**inputs).logits_per_image.softmax(dim=1)[0]
    best = probs.argmax().item()
    return labels[best], probs[best].item()


def _similarity(a: Path, b: Path) -> float:
    import imagehash
    import numpy as np
    from PIL import Image

    size = (224, 224)
    img1 = Image.open(a).convert("RGB").resize(size)
    img2 = Image.open(b).convert("RGB").resize(size)
    phash = 1 - (imagehash.phash(img1, hash_size=16) - imagehash.phash(img2, hash_size=16)) / 256
    ahash = 1 - (imagehash.average_hash(img1, hash_size=16) - imagehash.average_hash(img2, hash_size=16)) / 256
    h1 = np.array(img1.histogram(), dtype=float)
    h2 = np.array(img2.histogram(), dtype=float)
    hist = max(0.0, float(np.corrcoef(h1 / h1.sum(), h2 / h2.sum())[0, 1]))
    return phash * 0.5 + ahash * 0.3 + hist * 0.2


def _challenge_type(example: Path) -> dict:
    if KEYBOARD_REF.exists() and _similarity(example, KEYBOARD_REF) > SIMILARITY_THRESHOLD:
        return LABEL_CONFIGS["mouse_keyboard"]
    return LABEL_CONFIGS["bird_shelter"]


def _bg_url(style: str) -> str | None:
    start = style.find("url(")
    end = style.find(")", start)
    if start == -1 or end == -1:
        return None
    return style[start + 4 : end].strip("\"'").replace("&quot;", "")


async def _download(page: Page, url: str, dest: Path) -> bool:
    # Through the browser context, so the request carries the same cookies and fingerprint.
    try:
        resp = await page.context.request.get(url, timeout=15000)
        if not resp.ok:
            return False
        dest.write_bytes(await resp.body())
        return True
    except Exception as e:
        log.debug("tile download failed %s: %s", url, e)
        return False


async def _challenge_frame(page: Page) -> Frame | None:
    iframes = await page.query_selector_all(IFRAME)
    if len(iframes) < 2:
        return None
    return await iframes[1].content_frame()


async def _challenge_open(page: Page) -> bool:
    try:
        frame = await _challenge_frame(page)
        return bool(frame and await frame.query_selector_all("div.task-image"))
    except Exception:
        # The frame detaching mid-check means the challenge went away.
        return False


async def solve(page: Page) -> bool:
    """Try to pass the hCaptcha on `page`. Returns True if no challenge is left."""
    try:
        iframe = await page.wait_for_selector(IFRAME, timeout=10000)
        checkbox_frame = await iframe.content_frame()
        if not checkbox_frame:
            return False
        await (await checkbox_frame.wait_for_selector("#checkbox", timeout=10000)).click()
        log.info("captcha: checkbox clicked")
        await page.wait_for_timeout(2500)
    except Exception as e:
        log.warning("captcha: could not click checkbox: %s", e)
        return False

    if not await _challenge_open(page):
        return True

    with tempfile.TemporaryDirectory(prefix="baywatch_tiles_") as tmp:
        tmpdir = Path(tmp)
        for round_no in range(1, MAX_ROUNDS + 1):
            try:
                if await _round(page, tmpdir):
                    log.info("captcha: solved on round %d", round_no)
                    return True
            except Exception as e:
                log.warning("captcha: round %d error: %s", round_no, e)
            await page.wait_for_timeout(2000)
    log.warning("captcha: gave up after %d rounds", MAX_ROUNDS)
    return False


async def _round(page: Page, tmpdir: Path) -> bool:
    frame = await _challenge_frame(page)
    if not frame:
        return True

    example_div = await frame.wait_for_selector("div.challenge-example div.image", timeout=10000)
    example_url = _bg_url(await example_div.get_attribute("style") or "")
    example = tmpdir / "example.jpg"
    if not example_url or not await _download(page, example_url, example):
        log.warning("captcha: no example image")
        return False
    # CLIP runs on the CPU for a second or two per image; keep the event loop free for the other tabs.
    config = await asyncio.to_thread(_challenge_type, example)

    targets = []
    for i, tile in enumerate(await frame.query_selector_all("div.task-image")):
        img = await tile.query_selector(".image")
        url = _bg_url((await img.get_attribute("style") if img else "") or "")
        path = tmpdir / f"tile{i}.jpg"
        if not url or not await _download(page, url, path):
            continue
        label, score = await asyncio.to_thread(_classify, path, config["tile_labels"])
        if label == config["target_label"] and score > config["threshold"]:
            targets.append(i)
    log.info("captcha: looking for %r, matched tiles %s", config["target_label"], targets)
    if not targets:
        return False

    tiles = await frame.query_selector_all("div.task-image")
    for i in targets:
        await tiles[i].click()
        await page.wait_for_timeout(300)
    await page.wait_for_timeout(800)

    button = await frame.query_selector("div.button-submit.button") or await frame.query_selector("div.button-submit")
    if not button:
        log.warning("captcha: no verify button")
        return False
    await button.click()
    await page.wait_for_timeout(4000)
    return not await _challenge_open(page)
