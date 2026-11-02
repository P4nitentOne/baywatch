"""One real Chrome profile, several tabs working in parallel, one captcha at a time."""

from __future__ import annotations

import asyncio
import enum
import logging
import random
import re
import time
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Page, Playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from baywatch import captcha
from baywatch.config import Config

log = logging.getLogger(__name__)

CAPTCHA_FORM = "form[action*='captcha']"
# eBay's product photos, fonts and video. hCaptcha's tile images live elsewhere and still load.
HEAVY = re.compile(r"^https://i\.ebayimg\.com/|\.(woff2?|ttf|otf|mp4|webm)(\?|$)")


def _same_target(wanted: str, landed: str) -> bool:
    """eBay may add a title slug to item URLs or drop query params; the last path segment survives."""
    tail = urlsplit(wanted).path.rstrip("/").rsplit("/", 1)[-1]
    return tail in urlsplit(landed).path


class Outcome(enum.Enum):
    OK = "ok"
    BLOCKED = "blocked"      # eBay error page or a captcha nobody solved
    FAILED = "failed"        # timeout / network error


class Browser:
    """Launches one Chrome per worker, each with its own profile.

    eBay treats parallel tabs sharing one session as a bot (aborted loads, error
    pages), but separate profiles look like separate visitors. Each worker keeps
    its own cookies, captcha history and block cooldown.
    """

    def __init__(self, pw: Playwright, cfg: Config):
        self.pw = pw
        self.cfg = cfg
        self.contexts: list[BrowserContext] = []
        # Solve one captcha at a time: the CLIP model is shared and you only have one pair of hands.
        self.captcha_lock = asyncio.Lock()

    async def new_tab(self, n: int) -> Tab:
        profile = self.cfg.profile_dir / f"worker{n}"
        profile.mkdir(parents=True, exist_ok=True)
        ctx = await self.pw.chromium.launch_persistent_context(
            str(profile),
            channel=self.cfg.browser_channel,
            headless=False,  # eBay serves error pages to headless Chrome
            locale="it-IT" if self.cfg.site.endswith(".it") else None,
            viewport={"width": 1280, "height": 820},
            args=["--disable-blink-features=AutomationControlled", f"--window-position={40 * n},{40 * n}"],
            # Playwright's defaults; both show up as "this browser is automated".
            ignore_default_args=["--enable-automation", "--no-sandbox"],
        )
        if self.cfg.block_images:
            await ctx.route(HEAVY, lambda route: route.abort())
        self.contexts.append(ctx)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(30000)
        return Tab(self, ctx, page, n)

    async def close(self) -> None:
        for ctx in self.contexts:
            try:
                await ctx.close()
            except PlaywrightError:
                pass


class Tab:
    def __init__(self, browser: Browser, ctx: BrowserContext, page: Page, n: int):
        self.browser = browser
        self.cfg = browser.cfg
        self.ctx = ctx
        self.page = page
        self.n = n
        self.consecutive_blocks = 0
        self.cooldown_until = 0.0
        self.loads = 0

    def blocked(self) -> None:
        """One error page is noise; a streak means this session is flagged, so it slows down."""
        self.consecutive_blocks += 1
        p = self.cfg.pacing
        if self.consecutive_blocks <= p.free_retries:
            return
        n = self.consecutive_blocks - p.free_retries
        wait = min(p.backoff_base * 2 ** (n - 1), p.backoff_max)
        self.cooldown_until = time.monotonic() + wait
        log.warning("[w%d] blocked %d times in a row, pausing %.0fs", self.n, self.consecutive_blocks, wait)

    async def open(self, url: str, ready: str | None = None) -> Outcome:
        """Navigate to `url`, deal with captchas, and report whether we got the real page.

        `ready` is a selector worth waiting for; the page counts as loaded once it
        appears (or after a few seconds if it never does).
        """
        await self._pace()
        for attempt in range(3):
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
                break
            except PlaywrightTimeout:
                log.warning("timeout: %s", url)
                return Outcome.FAILED
            except PlaywrightError as e:
                if "ERR_ABORTED" in str(e) and not self.page.is_closed():
                    # Chrome dropped the navigation and the tab is still on the previous page.
                    # A short pause and a second try almost always goes through.
                    log.debug("aborted (try %d): %s", attempt + 1, url)
                    await asyncio.sleep(1.5 + attempt * 2)
                    continue
                log.warning("navigation error: %s (%s)", url, str(e).splitlines()[0])
                if "crashed" in str(e) or self.page.is_closed():
                    await self._replace_page()
                return Outcome.FAILED
        else:
            self.blocked()
            return Outcome.BLOCKED

        if await self.is_captcha() and not await self._clear_captcha(url):
            self.blocked()
            return Outcome.BLOCKED
        # "Sorry, something went wrong on our end" is an eBay bug that real users hit too
        # (community.ebay.co.uk topic 54401); refreshing fixes it. Refresh, don't back off.
        refreshes = 0
        while await self.is_error_page() and refreshes < self.cfg.pacing.error_refreshes:
            refreshes += 1
            log.debug("error page, refresh %d: %s", refreshes, url)
            await asyncio.sleep(random.uniform(0.5, 1.5))
            await self._reload(url)
        if await self.is_error_page():
            self.blocked()
            return Outcome.BLOCKED
        if not _same_target(url, self.page.url):
            # Bounced somewhere else (home page, sign-in): not the page we asked for.
            log.debug("bounced: %s -> %s", url, self.page.url)
            self.blocked()
            return Outcome.BLOCKED

        if ready:
            try:
                await self.page.wait_for_selector(ready, timeout=self.cfg.pacing.ready_timeout * 1000)
            except PlaywrightError:
                pass  # the caller decides what a missing element means
        self.consecutive_blocks = 0
        return Outcome.OK

    async def is_captcha(self) -> bool:
        try:
            return "captcha" in self.page.url.lower() or await self.page.locator(CAPTCHA_FORM).count() > 0
        except PlaywrightError:
            return False

    async def is_error_page(self) -> bool:
        try:
            return "Error Page" in await self.page.title()
        except PlaywrightError:
            return False

    async def hrefs(self, selector: str) -> list[str]:
        return await self._read(selector, "els => els.map(e => e.href).filter(Boolean)")

    async def texts(self, selector: str) -> list[str]:
        return await self._read(selector, "els => els.map(e => e.innerText)")

    async def links(self, selector: str) -> list[tuple[str, str]]:
        pairs = await self._read(selector, "els => els.map(e => [e.href, e.innerText])")
        return [(h, t) for h, t in pairs if h]

    async def read(self, selector: str, js: str) -> list:
        return await self._read(selector, js)

    async def count(self, selector: str) -> int:
        return len(await self._read(selector, "els => els.map(() => 1)"))

    # ---- internals ----------------------------------------------------------

    async def _read(self, selector: str, js: str) -> list:
        # A late client-side redirect can wipe the page mid-read; wait for it to land and read again.
        for _ in range(3):
            try:
                return await self.page.eval_on_selector_all(selector, js)
            except PlaywrightError as e:
                if "context was destroyed" not in str(e) and "navigat" not in str(e):
                    raise
                await self._settle()
        return []

    async def _settle(self) -> None:
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=15000)
        except PlaywrightError:
            pass
        await asyncio.sleep(0.5)

    async def _pace(self) -> None:
        p = self.cfg.pacing
        self.loads += 1
        if p.break_every and self.loads % p.break_every == 0:
            log.info("[w%d] short break: %.0fs", self.n, p.break_seconds)
            self.cooldown_until = max(self.cooldown_until, time.monotonic() + p.break_seconds)
        delay = self.cooldown_until - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        await asyncio.sleep(random.uniform(p.min_delay, p.max_delay))

    async def _replace_page(self) -> None:
        """A crashed tab rejects every later navigation, so swap it for a fresh one."""
        log.info("replacing crashed tab")
        old = self.page
        self.page = await self.ctx.new_page()
        self.page.set_default_timeout(30000)
        try:
            await old.close()
        except PlaywrightError:
            pass

    async def _clear_captcha(self, url: str) -> bool:
        async with self.browser.captcha_lock:
            if self.cfg.captcha_solver == "clip":
                log.info("[w%d] captcha: running automatic solver", self.n)
                try:
                    if await captcha.solve(self.page) and await self._wait_until_clear(30):
                        return await self._back_to(url)
                except captcha.SolverUnavailable as e:
                    log.error("%s; falling back to manual", e)
                except PlaywrightError as e:
                    log.warning("captcha: solver crashed: %s", str(e).splitlines()[0])

            log.warning(
                "captcha: solve it in the browser window, waiting up to %.0fs", self.cfg.pacing.captcha_timeout
            )
            try:
                await self.page.bring_to_front()
            except PlaywrightError:
                pass
            if await self._wait_until_clear(self.cfg.pacing.captcha_timeout):
                log.info("captcha: cleared, thanks")
                return await self._back_to(url)
            return False

    async def _reload(self, url: str) -> None:
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except PlaywrightError:
            pass

    async def _wait_until_clear(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not await self.is_captcha():
                return True
            await asyncio.sleep(2)
        return False

    async def _back_to(self, url: str) -> bool:
        """After a captcha eBay usually redirects to the original page; reload it if not."""
        await self.page.wait_for_timeout(1500)
        if self.page.url.split("?")[0] != url.split("?")[0]:
            await self._reload(url)
        return not await self.is_captcha() and not await self.is_error_page()
