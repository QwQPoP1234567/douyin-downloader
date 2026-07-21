from __future__ import annotations

import asyncio

from app.browser import BrowserManager
from app.config import Settings


class FakePage:
    def __init__(self, url: str = "about:blank"):
        self.url = url
        self.closed = False
        self.handlers: dict[str, object] = {}

    def is_closed(self) -> bool:
        return self.closed

    def on(self, event: str, handler: object) -> None:
        self.handlers[event] = handler

    async def goto(self, url: str) -> None:
        self.url = url

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, pages: list[FakePage]):
        self.pages = pages
        self.new_page_calls = 0

    async def new_page(self) -> FakePage:
        self.new_page_calls += 1
        page = FakePage()
        self.pages.append(page)
        return page


def test_anchor_reuses_persistent_context_initial_page(tmp_path):
    async def run() -> None:
        manager = BrowserManager(Settings(browser_data_dir=tmp_path / "browser"))
        initial = FakePage()
        context = FakeContext([initial])
        manager._context = context  # type: ignore[assignment]
        manager._owns_context = True

        await manager._ensure_anchor_page()
        closed = await manager.close_unused_pages()

        assert manager._anchor_page is initial
        assert context.new_page_calls == 0
        assert not initial.closed
        assert closed == 0

    asyncio.run(run())


def test_cleanup_closes_disposable_page_but_keeps_anchor(tmp_path):
    async def run() -> None:
        manager = BrowserManager(Settings(browser_data_dir=tmp_path / "browser"))
        anchor = FakePage()
        disposable = FakePage("https://www.douyin.com/")
        context = FakeContext([anchor, disposable])
        manager._context = context  # type: ignore[assignment]
        manager._anchor_page = anchor  # type: ignore[assignment]
        manager._retained_pages.add(anchor)  # type: ignore[arg-type]

        closed = await manager.close_unused_pages()

        assert not anchor.closed
        assert disposable.closed
        assert closed == 1

    asyncio.run(run())


def test_only_one_scan_page_can_be_opened_at_a_time(tmp_path):
    async def run() -> None:
        manager = BrowserManager(Settings(browser_data_dir=tmp_path / "browser"))
        anchor = FakePage()
        context = FakeContext([anchor])
        manager._context = context  # type: ignore[assignment]
        manager._anchor_page = anchor  # type: ignore[assignment]
        manager._retained_pages.add(anchor)  # type: ignore[arg-type]

        first = await manager.new_scan_page()
        second_opened = asyncio.Event()

        async def open_second() -> FakePage:
            page = await manager.new_scan_page()
            second_opened.set()
            return page  # type: ignore[return-value]

        task = asyncio.create_task(open_second())
        await asyncio.sleep(0)
        assert second_opened.is_set() is False

        await manager.release_page(first, keep_for_verification=False)  # type: ignore[arg-type]
        second = await task
        assert second_opened.is_set() is True
        await manager.release_page(second, keep_for_verification=False)  # type: ignore[arg-type]

    asyncio.run(run())
