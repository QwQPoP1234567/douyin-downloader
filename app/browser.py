from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from app.config import Settings


LOGIN_URL = "https://www.douyin.com/"
LOGIN_COOKIE_NAMES = {"sessionid", "sessionid_ss", "sid_guard", "uid_tt", "uid_tt_ss"}


class BrowserManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._owns_context = True
        self._start_lock = asyncio.Lock()
        self._scan_page_lock = asyncio.Lock()
        self._profile_tasks: set[asyncio.Task[Any]] = set()
        self._profile_api_verified = False
        self._profile_api_checked_at: str | None = None
        self._profile_nickname: str | None = None
        self._managed_pages: set[Page] = set()
        self._scan_pages: set[Page] = set()
        self._retained_pages: set[Page] = set()
        self._login_page: Page | None = None
        self._anchor_page: Page | None = None
        self._user_agent_cache: str | None = None

    @property
    def running(self) -> bool:
        return self._context is not None

    async def start(self) -> BrowserContext:
        async with self._start_lock:
            if self._context is not None:
                return self._context
            self.settings.browser_data_dir.mkdir(parents=True, exist_ok=True)
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None
            self._playwright = await async_playwright().start()
            if self.settings.browser_cdp_url:
                last_error: Exception | None = None
                for attempt in range(3):
                    try:
                        self._browser = await self._playwright.chromium.connect_over_cdp(
                            self.settings.browser_cdp_url
                        )
                        break
                    except Exception as exc:
                        last_error = exc
                        if attempt < 2:
                            continue
                if self._browser is None:
                    await self._playwright.stop()
                    self._playwright = None
                    raise RuntimeError(f"无法连接远程 Chrome CDP：{last_error}")
                if not self._browser.contexts:
                    await self._playwright.stop()
                    self._playwright = None
                    self._browser = None
                    raise RuntimeError("远程 Chrome 没有可用的默认浏览器上下文")
                self._context = self._browser.contexts[0]
                self._owns_context = False
                self._context.on("response", self._on_context_response)
                await self._ensure_anchor_page()
                await self.close_unused_pages()
                return self._context
            launch_options: dict[str, Any] = {
                "user_data_dir": str(self.settings.browser_data_dir),
                "headless": self.settings.browser_headless,
                "viewport": {"width": 1440, "height": 940},
                "locale": "zh-CN",
                "args": [
                    "--disable-notifications",
                ],
            }
            if self.settings.browser_channel:
                launch_options["channel"] = self.settings.browser_channel
            if self.settings.browser_proxy:
                launch_options["proxy"] = {"server": self.settings.browser_proxy}
            try:
                self._context = await self._playwright.chromium.launch_persistent_context(**launch_options)
            except Exception:
                # 系统 Chrome 不可用时立即回退到 Playwright Chromium。
                if launch_options.pop("channel", None):
                    try:
                        self._context = await self._playwright.chromium.launch_persistent_context(
                            **launch_options
                        )
                    except Exception:
                        await self._playwright.stop()
                        self._playwright = None
                        raise
                else:
                    await self._playwright.stop()
                    self._playwright = None
                    raise
            self._context.on("response", self._on_context_response)
            self._owns_context = True
            # A persistent Chromium context must keep at least one target alive.
            # Reuse the initial about:blank page as an anchor before the generic
            # cleanup runs; closing that last page makes Chrome exit while the
            # Python BrowserContext object can still look usable briefly.
            await self._ensure_anchor_page()
            await self.close_unused_pages()
            return self._context

    def _on_context_response(self, response: Any) -> None:
        if "/aweme/v1/web/user/profile/self/" not in response.url:
            return
        task = asyncio.create_task(self._inspect_self_profile(response))
        self._profile_tasks.add(task)
        task.add_done_callback(self._profile_tasks.discard)

    async def _inspect_self_profile(self, response: Any) -> None:
        try:
            payload = await response.json()
        except Exception:
            return
        self._profile_api_checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if not isinstance(payload, dict) or payload.get("status_code") != 0:
            self._profile_api_verified = False
            return

        def find_nickname(value: Any) -> str | None:
            if isinstance(value, dict):
                nickname = value.get("nickname")
                if isinstance(nickname, str) and nickname:
                    return nickname
                for child in value.values():
                    found = find_nickname(child)
                    if found:
                        return found
            elif isinstance(value, list):
                for child in value:
                    found = find_nickname(child)
                    if found:
                        return found
            return None

        self._profile_nickname = find_nickname(payload)
        # 成功的 self 响应并携带用户数据，是比单纯存在 Cookie 更强的登录证据。
        self._profile_api_verified = self._profile_nickname is not None or any(
            key in payload for key in ("user", "user_info", "data")
        )

    async def new_page(self) -> Page:
        context = await self.start()
        page = await context.new_page()
        self._managed_pages.add(page)

        def forget(*_: Any) -> None:
            self._managed_pages.discard(page)
            self._retained_pages.discard(page)
            if self._login_page is page:
                self._login_page = None

        page.on("close", forget)
        return page

    async def new_scan_page(self) -> Page:
        """Open the single globally permitted creator-profile scanning page."""
        await self._scan_page_lock.acquire()
        try:
            page = await self.new_page()
        except BaseException:
            self._scan_page_lock.release()
            raise
        self._scan_pages.add(page)

        def release_slot(*_: Any) -> None:
            self._release_scan_slot(page)

        page.on("close", release_slot)
        return page

    def _release_scan_slot(self, page: Page) -> None:
        if page not in self._scan_pages:
            return
        self._scan_pages.discard(page)
        if self._scan_page_lock.locked():
            self._scan_page_lock.release()

    async def _ensure_anchor_page(self) -> None:
        """Keep one blank target alive while disposable pages are closed."""
        if self._context is None:
            return
        if self._anchor_page is not None and not self._anchor_page.is_closed():
            self._retained_pages.add(self._anchor_page)
            return

        # launch_persistent_context creates an initial about:blank page. Reuse it
        # instead of closing the last target and then trying to create another.
        page = next(
            (
                candidate
                for candidate in self._context.pages
                if not candidate.is_closed()
                and candidate.url == "about:blank"
                and candidate not in self._managed_pages
                and candidate not in self._retained_pages
            ),
            None,
        )
        if page is None:
            page = await self._context.new_page()
            await page.goto("about:blank")
        self._anchor_page = page
        self._retained_pages.add(page)

        def forget_anchor(*_: Any) -> None:
            self._retained_pages.discard(page)
            if self._anchor_page is page:
                self._anchor_page = None

        page.on("close", forget_anchor)

    async def release_page(self, page: Page, keep_for_verification: bool = True) -> None:
        try:
            self._managed_pages.discard(page)
            if page.is_closed():
                self._retained_pages.discard(page)
                return
            if keep_for_verification and await page_needs_verification(page):
                self._retained_pages.add(page)
                await page.bring_to_front()
                return
            self._retained_pages.discard(page)
            await page.close()
            await self.close_unused_pages()
        finally:
            self._release_scan_slot(page)

    async def close_unused_pages(self) -> int:
        """Close stale app pages while preserving active tasks and human verification."""
        if self._context is None:
            return 0
        await self._ensure_anchor_page()
        pages = [page for page in self._context.pages if not page.is_closed()]
        for page in list(self._retained_pages):
            if page.is_closed():
                self._retained_pages.discard(page)
            elif (
                page is not self._login_page
                and page is not self._anchor_page
                and not await page_needs_verification(page)
            ):
                self._retained_pages.discard(page)

        candidates = [
            page
            for page in pages
            if page not in self._managed_pages and page not in self._retained_pages
        ]
        closed = 0
        for page in candidates:
            if await page_needs_verification(page):
                self._retained_pages.add(page)
                continue
            try:
                await page.close()
                closed += 1
            except Exception:
                continue
        return closed

    async def open_login(self) -> Page:
        context = await self.start()
        for existing_page in reversed(context.pages):
            if await page_needs_verification(existing_page):
                self._retained_pages.add(existing_page)
                await existing_page.bring_to_front()
                return existing_page
        available = [
            candidate
            for candidate in context.pages
            if candidate not in self._managed_pages
            and candidate not in self._retained_pages
            and candidate is not self._anchor_page
            and not candidate.is_closed()
        ]
        page = available[0] if available else await context.new_page()
        self._retained_pages.add(page)
        self._login_page = page
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    continue
        if last_error:
            raise last_error
        await page.bring_to_front()
        return page

    async def login_status(self) -> dict[str, Any]:
        if self._context is None:
            return {"browser_running": False, "logged_in": False}
        try:
            cookies = await self._context.cookies(["https://www.douyin.com/"])
        except Exception:
            self._context = None
            self._browser = None
            self._owns_context = True
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None
            return {"browser_running": False, "logged_in": False}
        present = {cookie["name"] for cookie in cookies}
        logged_in = self._profile_api_verified or bool(present & LOGIN_COOKIE_NAMES)
        verification_required = False
        for page in self._context.pages:
            if await page_needs_verification(page):
                verification_required = True
                break
        if (
            logged_in
            and self._login_page is not None
            and not self._login_page.is_closed()
            and not await page_needs_verification(self._login_page)
        ):
            self._retained_pages.discard(self._login_page)
            self._login_page = None
        closed_unused_pages = await self.close_unused_pages()
        return {
            "browser_running": True,
            "logged_in": logged_in,
            "verification_required": verification_required,
            "verified_by_profile_api": self._profile_api_verified,
            "profile_api_checked_at": self._profile_api_checked_at,
            "profile_nickname": self._profile_nickname,
            "login_cookie_names": sorted(present & LOGIN_COOKIE_NAMES),
            "closed_unused_pages": closed_unused_pages,
        }

    async def cookie_dict(self) -> dict[str, str]:
        context = await self.start()
        cookies = await context.cookies()
        return {cookie["name"]: cookie["value"] for cookie in cookies}

    async def user_agent(self) -> str:
        if self._user_agent_cache:
            return self._user_agent_cache
        context = await self.start()
        existing = next(
            (page for page in context.pages if not page.is_closed()),
            None,
        )
        if existing is not None:
            self._user_agent_cache = await existing.evaluate("navigator.userAgent")
            return self._user_agent_cache
        page = await self.new_page()
        try:
            self._user_agent_cache = await page.evaluate("navigator.userAgent")
            return self._user_agent_cache
        finally:
            await self.release_page(page, keep_for_verification=False)

    async def close(self) -> None:
        if self._profile_tasks:
            await asyncio.gather(*list(self._profile_tasks), return_exceptions=True)
            self._profile_tasks.clear()
        if self._context is not None and self._owns_context:
            try:
                await self._context.close()
            finally:
                self._context = None
        else:
            # CDP 模式只断开 Playwright，不关闭外部常驻 Chrome。
            self._context = None
        self._managed_pages.clear()
        self._scan_pages.clear()
        if self._scan_page_lock.locked():
            self._scan_page_lock.release()
        self._retained_pages.clear()
        self._login_page = None
        self._anchor_page = None
        self._user_agent_cache = None
        self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None


async def page_needs_verification(page: Page) -> bool:
    url = page.url.lower()
    if any(marker in url for marker in ("captcha", "verify", "security-check")):
        return True
    try:
        title = await page.title()
        if any(marker in title for marker in ("验证码", "验证中间页", "安全验证", "验证中心")):
            return True
    except Exception:
        pass
    selectors = [
        "iframe[src*='captcha']",
        "[class*='captcha_verify']",
        "[id*='captcha']",
        "text=请完成下列验证",
        "text=安全验证",
        "text=请完成验证",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() and await locator.is_visible(timeout=500):
                return True
        except Exception:
            continue
    return False
