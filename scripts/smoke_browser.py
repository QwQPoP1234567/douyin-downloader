from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.browser import BrowserManager, page_needs_verification
from app.config import Settings


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="douyin-browser-smoke-") as temp:
        root = Path(temp)
        settings = Settings(
            data_dir=root / "data",
            download_dir=root / "downloads",
            browser_data_dir=root / "browser",
            browser_headless=True,
        )
        browser = BrowserManager(settings)
        try:
            context = await browser.start()
            page = context.pages[0] if context.pages else await context.new_page()
            response = await page.goto(
                "https://www.douyin.com/",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            print(
                {
                    "ok": bool(response and response.status < 500),
                    "status": response.status if response else None,
                    "url": page.url,
                    "title": await page.title(),
                    "title_unicode": (await page.title()).encode("unicode_escape").decode("ascii"),
                    "needs_verification": await page_needs_verification(page),
                }
            )
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
