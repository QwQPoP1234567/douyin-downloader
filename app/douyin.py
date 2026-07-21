from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Literal
from urllib.parse import parse_qs, unquote, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Response, TimeoutError as PlaywrightTimeoutError

from app.browser import BrowserManager, page_needs_verification
from app.config import Settings


WORK_ID_RE = re.compile(r"/(?:video|note)/(\d+)")
ALLOWED_HOST_SUFFIXES = ("douyin.com", "iesdouyin.com")


class VerificationRequired(RuntimeError):
    pass


class InvalidProfileUrl(ValueError):
    pass


@dataclass
class ScanResult:
    profile_url: str
    nickname: str | None
    sec_uid: str | None
    videos: list[dict[str, Any]]
    complete: bool = True
    discarded_count: int = 0
    expected_count: int | None = None
    aweme_ids: list[str] | None = None
    encountered_aweme_ids: list[str] | None = None
    latest_create_time: int | None = None
    avatar_url: str | None = None
    scroll_count: int = 0
    cursor: str | None = None
    stop_reason: str | None = None


ScanBatchCallback = Callable[[list[dict[str, Any]]], Awaitable[None]]
ScanProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]
ScanControlCallback = Callable[[], Awaitable[Literal["pause", "cancel"] | None]]


class ScanBatchAccumulator:
    """Deduplicate scan results and release full payloads after each persisted batch."""

    def __init__(
        self,
        *,
        item_limit: int,
        batch_size: int,
        on_batch: ScanBatchCallback | None,
        ignored_ids: set[str] | None = None,
    ):
        self.item_limit = max(1, item_limit)
        self.batch_size = max(1, min(batch_size, self.item_limit))
        self.on_batch = on_batch
        self.ignored_ids = ignored_ids or set()
        self.seen_ids: set[str] = set()
        self.encountered_ids: set[str] = set()
        self.encountered_aweme_ids: list[str] = []
        self.aweme_ids: list[str] = []
        self.pending: list[dict[str, Any]] = []
        self.collected: list[dict[str, Any]] = []
        self.latest_create_time: int | None = None
        self.nickname: str | None = None
        self.sec_uid: str | None = None
        self.avatar_url: str | None = None

    @property
    def reached_limit(self) -> bool:
        return len(self.aweme_ids) >= self.item_limit

    async def add(self, items: Iterable[dict[str, Any]]) -> None:
        for item in items:
            aweme_id = str(item.get("aweme_id") or "")
            if not aweme_id:
                continue
            if aweme_id not in self.encountered_ids:
                self.encountered_ids.add(aweme_id)
                self.encountered_aweme_ids.append(aweme_id)
            if (
                aweme_id in self.ignored_ids
                or aweme_id in self.seen_ids
                or self.reached_limit
            ):
                continue
            self.seen_ids.add(aweme_id)
            self.aweme_ids.append(aweme_id)
            self.pending.append(item)
            if self.nickname is None and item.get("nickname"):
                self.nickname = str(item["nickname"])
            if self.sec_uid is None and item.get("sec_uid"):
                self.sec_uid = str(item["sec_uid"])
            if self.avatar_url is None and item.get("avatar_url"):
                self.avatar_url = str(item["avatar_url"])
            if item.get("create_time"):
                timestamp = int(item["create_time"])
                self.latest_create_time = max(self.latest_create_time or timestamp, timestamp)
            if len(self.pending) >= self.batch_size:
                await self.flush()

    async def flush(self) -> None:
        if not self.pending:
            return
        batch = self.pending
        self.pending = []
        if self.on_batch is None:
            self.collected.extend(batch)
        else:
            await self.on_batch(batch)


def validate_profile_url(url: str) -> str:
    value = url.strip()
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not any(
        host == suffix or host.endswith(f".{suffix}") for suffix in ALLOWED_HOST_SUFFIXES
    ):
        raise InvalidProfileUrl("请输入有效的抖音用户主页或抖音短链接")
    return value


def _url_list(value: Any) -> list[str]:
    if isinstance(value, str) and value.startswith("http"):
        return [value]
    if not isinstance(value, dict):
        return []
    values = value.get("url_list") or value.get("urlList") or []
    return [
        url.replace("/playwm/", "/play/")
        for url in values
        if isinstance(url, str) and url.startswith("http")
    ]


def _first_url(*values: Any) -> str | None:
    for value in values:
        urls = _url_list(value)
        if urls:
            return urls[0]
    return None


def image_asset_urls(item: dict[str, Any]) -> list[str]:
    """Return one watermark-free source URL for each image in a note/image post."""
    containers: Any = (
        item.get("images")
        or item.get("image_list")
        or item.get("imageList")
        or item.get("original_images")
    )
    image_post = item.get("image_post_info") or item.get("imagePostInfo")
    if not containers and isinstance(image_post, dict):
        containers = image_post.get("images") or image_post.get("image_list")
    if not isinstance(containers, list):
        return []

    result: list[str] = []
    for image in containers:
        if not isinstance(image, dict):
            continue
        candidates: list[str] = []
        for key in (
            "watermark_free_download_url_list",
            "watermarkFreeDownloadUrlList",
            "url_list",
            "urlList",
        ):
            values = image.get(key)
            if isinstance(values, list):
                candidates.extend(
                    value
                    for value in values
                    if isinstance(value, str) and value.startswith("http")
                )
        selected = next(
            (
                value
                for value in candidates
                if "dy-water" not in value and "/playwm/" not in value
            ),
            None,
        )
        if selected:
            result.append(selected)
    return list(dict.fromkeys(result))


def _compact_url_container(value: Any) -> dict[str, Any] | None:
    urls = _url_list(value)
    if not urls:
        return None
    result: dict[str, Any] = {"url_list": urls}
    if isinstance(value, dict):
        for key in ("width", "height", "data_size"):
            if value.get(key) is not None:
                result[key] = value[key]
    return result


def compact_aweme_raw(
    *,
    video: dict[str, Any],
    sorted_rates: list[dict[str, Any]],
    image_urls: list[str],
    author: dict[str, Any],
    is_daily: bool,
) -> dict[str, Any]:
    """Keep only fields required for media fallback and ownership checks."""
    result: dict[str, Any] = {}
    sec_uid = author.get("sec_uid") or author.get("secUid")
    if sec_uid:
        result["author"] = {"sec_uid": sec_uid}
    if image_urls:
        result["images"] = [{"url_list": [url]} for url in image_urls]
    else:
        compact_video: dict[str, Any] = {}
        compact_rates = []
        for entry in sorted_rates:
            address = _compact_url_container(
                entry.get("play_addr") or entry.get("playAddr")
            )
            if not address:
                continue
            compact_entry: dict[str, Any] = {"play_addr": address}
            for source in ("FPS", "fps", "bit_rate", "bitRate"):
                if entry.get(source) is not None:
                    compact_entry[source] = entry[source]
            compact_rates.append(compact_entry)
        if compact_rates:
            compact_video["bit_rate"] = compact_rates
        for source in (
            "play_addr_h264",
            "playAddrH264",
            "play_addr",
            "playAddr",
        ):
            address = _compact_url_container(video.get(source))
            if address:
                compact_video[
                    "play_addr_h264" if "h264" in source.lower() else "play_addr"
                ] = address
        if compact_video:
            result["video"] = compact_video
    if is_daily:
        result["is_story"] = True
    return result


def parse_aweme(item: dict[str, Any]) -> dict[str, Any] | None:
    aweme_id = item.get("aweme_id") or item.get("awemeId") or item.get("id")
    video = item.get("video")
    images = image_asset_urls(item)
    if not aweme_id or (not isinstance(video, dict) and not images):
        return None
    video = video if isinstance(video, dict) else {}
    bit_rates = video.get("bit_rate") or video.get("bitRate") or []
    bit_rate_urls: list[Any] = []
    sorted_rates: list[dict[str, Any]] = []
    if isinstance(bit_rates, list):
        sorted_rates = sorted(
            (entry for entry in bit_rates if isinstance(entry, dict)),
            key=lambda entry: (
                max(
                    int((entry.get("play_addr") or entry.get("playAddr") or {}).get("height") or 0),
                    int((entry.get("play_addr") or entry.get("playAddr") or {}).get("width") or 0),
                ),
                int(entry.get("FPS") or entry.get("fps") or 0),
                int(entry.get("bit_rate") or entry.get("bitRate") or 0),
                int((entry.get("play_addr") or entry.get("playAddr") or {}).get("data_size") or 0),
            ),
            reverse=True,
        )
        bit_rate_urls = [entry.get("play_addr") or entry.get("playAddr") for entry in sorted_rates]
    content_type = "images" if images else "video"
    # Image posts expose their soundtrack through video.play_addr. It is audio,
    # not a video asset, so never save it as an .mp4.
    play_url = None if images else _first_url(
        *bit_rate_urls,
        video.get("play_addr_h264"),
        video.get("playAddrH264"),
        video.get("play_addr"),
        video.get("playAddr"),
    )
    cover_url = _first_url(
        video.get("origin_cover"),
        video.get("originCover"),
        video.get("cover"),
        video.get("dynamic_cover"),
        video.get("dynamicCover"),
    )
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    is_daily = bool(
        item.get("is_story")
        or item.get("is_moment_story")
        or item.get("is_24_story")
        or item.get("is_25_story")
    )
    return {
        "aweme_id": str(aweme_id),
        "description": item.get("desc") or item.get("description") or "",
        "create_time": item.get("create_time") or item.get("createTime"),
        "video_url": play_url,
        "cover_url": cover_url or (images[0] if images else None),
        "share_url": (
            f"https://www.douyin.com/note/{aweme_id}"
            if images
            else item.get("share_url") or f"https://www.douyin.com/video/{aweme_id}"
        ),
        "nickname": author.get("nickname"),
        "sec_uid": author.get("sec_uid") or author.get("secUid"),
        "avatar_url": _first_url(
            author.get("avatar_thumb"),
            author.get("avatarThumb"),
            author.get("avatar_medium"),
            author.get("avatarMedium"),
            author.get("avatar_larger"),
            author.get("avatarLarger"),
        ),
        "content_type": content_type,
        "asset_count": len(images) if images else 1,
        "is_daily": is_daily,
        "image_urls": images,
        "raw": compact_aweme_raw(
            video=video,
            sorted_rates=sorted_rates,
            image_urls=images,
            author=author,
            is_daily=is_daily,
        ),
    }


def find_aweme_items(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        parsed = parse_aweme(value)
        if parsed:
            yield parsed
            return
        for child in value.values():
            yield from find_aweme_items(child)
    elif isinstance(value, list):
        for child in value:
            yield from find_aweme_items(child)


def filter_profile_items(
    items: Iterable[dict[str, Any]], target_sec_uid: str | None
) -> list[dict[str, Any]]:
    values = list(items)
    if not target_sec_uid:
        return values
    return [item for item in values if item.get("sec_uid") == target_sec_uid]


class DouyinScanner:
    def __init__(self, browser: BrowserManager, settings: Settings):
        self.browser = browser
        self.settings = settings

    async def _goto(self, page: Page, url: str) -> None:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                return
            except (PlaywrightTimeoutError, PlaywrightError) as exc:
                last_error = exc
                if attempt < 2:
                    continue
        if last_error:
            raise last_error

    async def _capture_response(
        self,
        response: Response,
        found: dict[str, dict[str, Any]],
        risk_reasons: list[str] | None = None,
    ) -> None:
        url = response.url.lower()
        if not any(marker in url for marker in ("aweme", "post", "detail", "feed")):
            return
        if response.status in {403, 412, 429} and risk_reasons is not None:
            risk_reasons.append(f"接口返回 HTTP {response.status}")
        try:
            content_type = (await response.header_value("content-type") or "").lower()
        except Exception:
            return
        if "json" not in content_type:
            return
        try:
            data = await response.json()
        except Exception:
            return
        if isinstance(data, dict) and risk_reasons is not None:
            for key in ("status_msg", "message", "prompts", "description"):
                value = data.get(key)
                if isinstance(value, str) and any(
                    marker in value.lower()
                    for marker in ("captcha", "verify", "验证", "风控", "访问频繁", "请求频繁")
                ):
                    risk_reasons.append(value[:300])
            if any(key in data for key in ("captcha", "verify_data", "verify_center_decision_conf")):
                risk_reasons.append("接口响应包含安全验证数据")
        try:
            all_headers = await response.request.all_headers()
        except Exception:
            all_headers = {}
        replay_headers = {
            key: value
            for key, value in all_headers.items()
            if not key.startswith(":") and key.lower() not in {
                "cookie", "host", "content-length", "connection", "accept-encoding"
            }
        }
        for item in find_aweme_items(data):
            if isinstance(item.get("raw"), dict):
                item["raw"]["_network_headers"] = replay_headers
            found[item["aweme_id"]] = item

    async def _capture_profile_response(
        self,
        response: Response,
        captures: dict[str, dict[str, Any]],
        risk_reasons: list[str],
    ) -> None:
        parsed_url = urlparse(response.url)
        if "/aweme/v1/web/aweme/post/" not in parsed_url.path:
            return
        query = parse_qs(parsed_url.query)
        sec_uid = (query.get("sec_user_id") or [None])[0]
        if not sec_uid:
            return
        if response.status in {403, 412, 429}:
            risk_reasons.append(f"主页作品接口返回 HTTP {response.status}")
        content_type = (await response.header_value("content-type") or "").lower()
        if "json" not in content_type:
            return
        try:
            data = await response.json()
        except Exception:
            return
        if not isinstance(data, dict):
            return
        for key in ("status_msg", "message", "prompts", "description"):
            value = data.get(key)
            if isinstance(value, str) and any(
                marker in value.lower()
                for marker in ("captcha", "verify", "验证", "风控", "访问频繁", "请求频繁")
            ):
                risk_reasons.append(value[:300])
        if any(key in data for key in ("captcha", "verify_data", "verify_center_decision_conf")):
            risk_reasons.append("主页作品接口包含安全验证数据")

        state = captures.setdefault(
            sec_uid,
            {
                "found": {},
                "seen_cursors": set(),
                "pages": 0,
                "has_more": None,
                "max_cursor": None,
                "discarded": 0,
            },
        )
        request_cursor = str((query.get("max_cursor") or ["0"])[0])
        if request_cursor not in state["seen_cursors"]:
            state["seen_cursors"].add(request_cursor)
            state["pages"] += 1
        try:
            all_headers = await response.request.all_headers()
        except Exception:
            all_headers = {}
        replay_headers = {
            key: value
            for key, value in all_headers.items()
            if not key.startswith(":") and key.lower() not in {
                "cookie", "host", "content-length", "connection", "accept-encoding"
            }
        }
        values = data.get("aweme_list") or data.get("awemeList") or []
        if isinstance(values, list):
            for raw in values:
                if not isinstance(raw, dict):
                    continue
                item = parse_aweme(raw)
                if not item:
                    continue
                if item.get("sec_uid") and item.get("sec_uid") != sec_uid:
                    state["discarded"] += 1
                    continue
                if isinstance(item.get("raw"), dict):
                    item["raw"]["_network_headers"] = replay_headers
                state["found"][item["aweme_id"]] = item
        if "has_more" in data:
            raw_has_more = data.get("has_more")
            try:
                state["has_more"] = bool(int(raw_has_more))
            except (TypeError, ValueError):
                state["has_more"] = bool(raw_has_more)
        state["max_cursor"] = data.get("max_cursor")

    async def _collect_dom_links(
        self,
        page: Page,
        found: dict[str, dict[str, Any]],
        target_sec_uid: str | None = None,
    ) -> int:
        try:
            links = await page.locator('a[href*="/video/"], a[href*="/note/"]').evaluate_all(
                "els => els.map(el => el.href)"
            )
        except Exception:
            return 0
        before = len(found)
        for link in links:
            match = WORK_ID_RE.search(str(link))
            if not match:
                continue
            aweme_id = match.group(1)
            is_note = "/note/" in str(link)
            found.setdefault(
                aweme_id,
                {
                    "aweme_id": aweme_id,
                    "description": "",
                    "create_time": None,
                    "video_url": None,
                    "cover_url": None,
                    "share_url": (
                        f"https://www.douyin.com/note/{aweme_id}"
                        if is_note
                        else f"https://www.douyin.com/video/{aweme_id}"
                    ),
                    "nickname": None,
                    "sec_uid": target_sec_uid,
                    "content_type": "images" if is_note else "video",
                    "asset_count": 0 if is_note else 1,
                    "is_daily": False,
                    "image_urls": [],
                    "raw": {},
                },
            )
        return len(found) - before

    async def _profile_expected_count(self, page: Page) -> int | None:
        try:
            labels = await page.locator('[role="tab"]').all_inner_texts()
        except Exception:
            return None
        for label in labels:
            match = re.search(r"作品\s*(\d+)", label)
            if match:
                return int(match.group(1))
        return None

    async def _scroll_profile_container(self, page: Page) -> dict[str, int]:
        try:
            result = await page.evaluate(
                """
                () => {
                    const preferred = document.querySelector('.route-scroll-container');
                    const candidates = [...document.querySelectorAll('div')]
                        .filter(el => el.scrollHeight > el.clientHeight + 120)
                        .sort((a, b) =>
                            (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
                    const target = preferred || candidates[0];
                    if (!target) {
                        const before = window.scrollY;
                        window.scrollTo(0, document.documentElement.scrollHeight);
                        return {before, after: window.scrollY, height: document.documentElement.scrollHeight};
                    }
                    const before = target.scrollTop;
                    const height = target.scrollHeight;
                    target.scrollTop = height;
                    target.dispatchEvent(new Event('scroll', {bubbles: true}));
                    return {before, after: target.scrollTop, height};
                }
                """
            )
        except Exception:
            return {"before": -1, "after": -1, "height": -1}
        return {
            "before": int(result.get("before") or 0),
            "after": int(result.get("after") or 0),
            "height": int(result.get("height") or 0),
        }

    async def _collect_embedded_json(
        self, page: Page, found: dict[str, dict[str, Any]]
    ) -> int:
        try:
            scripts = await page.locator("script").all_text_contents()
        except Exception:
            return 0
        before = len(found)
        for script in scripts:
            if not script or not any(marker in script for marker in ("aweme_id", "awemeId")):
                continue
            text = script.strip()
            if len(text) > 25_000_000:
                continue
            candidates = [text]
            if text.startswith("%7B") or text.startswith("%7b"):
                candidates.insert(0, unquote(text))
            first_brace, last_brace = text.find("{"), text.rfind("}")
            if first_brace >= 0 and last_brace > first_brace:
                candidates.append(text[first_brace : last_brace + 1])
            for candidate in candidates:
                try:
                    data = json.loads(candidate)
                except (json.JSONDecodeError, TypeError):
                    continue
                for item in find_aweme_items(data):
                    found[item["aweme_id"]] = item
                break
        return len(found) - before

    async def scan_profile(
        self,
        profile_url: str,
        *,
        item_limit: int | None = None,
        batch_size: int | None = None,
        max_scrolls: int | None = None,
        max_runtime_seconds: int | None = None,
        no_progress_seconds: int | None = None,
        on_batch: ScanBatchCallback | None = None,
        on_progress: ScanProgressCallback | None = None,
        check_control: ScanControlCallback | None = None,
        skip_aweme_ids: set[str] | None = None,
    ) -> ScanResult:
        profile_url = validate_profile_url(profile_url)
        page = await self.browser.new_scan_page()
        embedded_found: dict[str, dict[str, Any]] = {}
        dom_fallback: dict[str, dict[str, Any]] = {}
        profile_captures: dict[str, dict[str, Any]] = {}
        response_tasks: set[asyncio.Task[Any]] = set()
        risk_reasons: list[str] = []
        limit = max(1, int(item_limit or 100))
        accumulator = ScanBatchAccumulator(
            item_limit=limit,
            batch_size=int(batch_size or self.settings.scan_batch_size),
            on_batch=on_batch,
            ignored_ids=skip_aweme_ids,
        )
        scroll_limit = max(1, int(max_scrolls or self.settings.max_scan_scrolls))
        runtime_limit = max(
            1, int(max_runtime_seconds or self.settings.scan_max_runtime_seconds)
        )
        no_progress_limit = max(
            1, int(no_progress_seconds or self.settings.scan_no_progress_seconds)
        )
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        last_progress_at = started_at
        last_progress_signature: tuple[int, int, str | None] | None = None
        scroll_count = 0
        stop_reason: str | None = None
        resolved_url = profile_url
        expected_count: int | None = None
        target_sec_uid: str | None = None
        complete = False

        def on_response(response: Response) -> None:
            task = asyncio.create_task(
                self._capture_profile_response(response, profile_captures, risk_reasons)
            )
            response_tasks.add(task)
            task.add_done_callback(response_tasks.discard)

        page.on("response", on_response)
        try:
            await self._goto(page, profile_url)
            await page.wait_for_timeout(1800)
            if await page_needs_verification(page):
                await page.bring_to_front()
                raise VerificationRequired("抖音要求人工完成安全验证")
            await self._collect_embedded_json(page, embedded_found)
            resolved_url = page.url
            if "/user/" not in resolved_url:
                # 短链接可能先进入作品页，再从作者入口跳转；保留页面以便用户检查。
                user_link = page.locator('a[href*="/user/"]').first
                if await user_link.count():
                    candidate = await user_link.get_attribute("href")
                    if candidate:
                        resolved_url = candidate if candidate.startswith("http") else f"https://www.douyin.com{candidate}"
                        await self._goto(page, resolved_url)
                        await page.wait_for_timeout(1200)

            match = re.search(r"/user/([^/?]+)", resolved_url)
            target_sec_uid = match.group(1) if match else None
            expected_count = await self._profile_expected_count(page)
            await accumulator.add(filter_profile_items(embedded_found.values(), target_sec_uid))
            embedded_found.clear()

            stable_rounds = 0
            previous_signature: tuple[int, int, int, int] | None = None
            for scroll_count in range(1, scroll_limit + 1):
                if loop.time() - started_at >= runtime_limit:
                    stop_reason = "runtime_limit"
                    break
                if response_tasks:
                    await asyncio.gather(*list(response_tasks), return_exceptions=True)
                state = profile_captures.get(target_sec_uid or "")
                if state:
                    captured = list(state["found"].values())
                    state["found"].clear()
                    await accumulator.add(filter_profile_items(captured, target_sec_uid))
                else:
                    # DOM links are only a last-resort fallback. Once the authoritative profile
                    # endpoint has responded, page-level links may include recommendation cards
                    # from other authors and must not be merged into the target account.
                    await self._collect_dom_links(page, dom_fallback, target_sec_uid)
                if risk_reasons:
                    await page.bring_to_front()
                    raise VerificationRequired(risk_reasons[-1])
                if await page_needs_verification(page):
                    await page.bring_to_front()
                    raise VerificationRequired("扫描过程中出现安全验证，请在浏览器完成后重试")
                expected_count = expected_count or await self._profile_expected_count(page)
                has_more = state.get("has_more") if state else None
                pages = int(state.get("pages") or 0) if state else 0
                cursor = str(state.get("max_cursor")) if state and state.get("max_cursor") is not None else None
                progress_signature = (len(accumulator.aweme_ids), pages, cursor)
                if progress_signature != last_progress_signature:
                    last_progress_signature = progress_signature
                    last_progress_at = loop.time()
                if on_progress is not None:
                    await on_progress(
                        {
                            "scroll_count": scroll_count,
                            "discovered_count": len(accumulator.aweme_ids),
                            "encountered_count": len(accumulator.encountered_aweme_ids),
                            "page_count": pages,
                            "cursor": cursor,
                            "has_more": has_more,
                            "expected_count": expected_count,
                            "elapsed_seconds": int(loop.time() - started_at),
                        }
                    )
                if check_control is not None:
                    control = await check_control()
                    if control is not None:
                        stop_reason = control
                        break
                if accumulator.reached_limit:
                    stop_reason = "item_limit"
                    break
                if has_more is False and (
                    expected_count is None
                    or len(accumulator.encountered_aweme_ids) >= expected_count
                ):
                    complete = True
                    stop_reason = "complete"
                    break

                scroll = await self._scroll_profile_container(page)
                signature = (
                    len(accumulator.aweme_ids),
                    pages,
                    scroll["after"],
                    scroll["height"],
                )
                if signature == previous_signature:
                    stable_rounds += 1
                else:
                    stable_rounds = 0
                previous_signature = signature

                # A positive has_more is authoritative: never call the scan complete merely
                # because the virtualized DOM stopped changing. Stop only as incomplete after
                # several recovery attempts so deletion/private reconciliation is not run.
                stable_limit = max(2, int(self.settings.scan_stable_rounds))
                if has_more is True and stable_rounds >= stable_limit * 3:
                    stop_reason = "no_progress"
                    break
                if has_more is None and stable_rounds >= stable_limit:
                    complete = bool(
                        expected_count is not None
                        and len(accumulator.encountered_aweme_ids) >= expected_count
                    )
                    stop_reason = "complete" if complete else "no_progress"
                    break
                if loop.time() - last_progress_at >= no_progress_limit:
                    stop_reason = "no_progress"
                    break
                await page.wait_for_timeout(
                    int(self.settings.scan_scroll_wait_ms * random.uniform(0.75, 1.35))
                )
            else:
                stop_reason = "max_scrolls"

            if response_tasks:
                await asyncio.gather(*list(response_tasks), return_exceptions=True)
            state = profile_captures.get(target_sec_uid or "")
            if state:
                captured = list(state["found"].values())
                state["found"].clear()
                await accumulator.add(filter_profile_items(captured, target_sec_uid))
            if not accumulator.aweme_ids and dom_fallback:
                await accumulator.add(filter_profile_items(dom_fallback.values(), target_sec_uid))
            await accumulator.flush()
            nickname = accumulator.nickname
            sec_uid = target_sec_uid or accumulator.sec_uid
            if not sec_uid:
                match = re.search(r"/user/([^/?]+)", resolved_url)
                sec_uid = match.group(1) if match else None
            if not nickname:
                title = (await page.title()).strip()
                nickname = title.split("-")[0].strip() if title else None
            return ScanResult(
                resolved_url,
                nickname,
                sec_uid,
                accumulator.collected,
                complete=complete,
                discarded_count=(
                    int(state.get("discarded") or 0) if state else 0
                ),
                expected_count=expected_count,
                aweme_ids=accumulator.aweme_ids,
                encountered_aweme_ids=accumulator.encountered_aweme_ids,
                latest_create_time=accumulator.latest_create_time,
                avatar_url=accumulator.avatar_url,
                scroll_count=scroll_count,
                cursor=(
                    str(state.get("max_cursor"))
                    if state and state.get("max_cursor") is not None
                    else None
                ),
                stop_reason=stop_reason,
            )
        finally:
            page.remove_listener("response", on_response)
            if response_tasks:
                for task in list(response_tasks):
                    task.cancel()
                await asyncio.gather(*list(response_tasks), return_exceptions=True)
            # 出现验证码时保留页面，其他扫描页立即释放。
            await self.browser.release_page(page, keep_for_verification=True)

    async def resolve_video(
        self,
        aweme_id: str,
        share_url: str | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any] | None:
        page = await self.browser.new_page()
        found: dict[str, dict[str, Any]] = {}
        media: dict[str, Any] = {}
        response_tasks: set[asyncio.Task[Any]] = set()
        risk_reasons: list[str] = []

        def on_response(response: Response) -> None:
            async def capture() -> None:
                await self._capture_response(response, found, risk_reasons)
                try:
                    content_type = (await response.header_value("content-type") or "").lower()
                    if response.status not in {200, 206} or not content_type.startswith("video/"):
                        return
                    headers = await response.request.all_headers()
                    media["url"] = response.url
                    media["headers"] = {
                        key: value
                        for key, value in headers.items()
                        if not key.startswith(":") and key.lower() not in {
                            "cookie", "host", "content-length", "connection", "accept-encoding"
                        }
                    }
                except Exception:
                    return

            task = asyncio.create_task(capture())
            response_tasks.add(task)
            task.add_done_callback(response_tasks.discard)

        page.on("response", on_response)
        try:
            detail_url = share_url if isinstance(share_url, str) and WORK_ID_RE.search(share_url) else None
            if not detail_url:
                kind = "note" if content_type == "images" else "video"
                detail_url = f"https://www.douyin.com/{kind}/{aweme_id}"
            await self._goto(page, detail_url)
            await page.wait_for_timeout(1600)
            if response_tasks:
                await asyncio.gather(*list(response_tasks), return_exceptions=True)
            if risk_reasons:
                await page.bring_to_front()
                raise VerificationRequired(risk_reasons[-1])
            if await page_needs_verification(page):
                await page.bring_to_front()
                raise VerificationRequired("获取视频地址时出现安全验证")
            item = found.get(str(aweme_id))
            if item and media.get("url"):
                item["video_url"] = media["url"]
                if isinstance(item.get("raw"), dict):
                    item["raw"]["_network_headers"] = media.get("headers", {})
            if item and (item.get("video_url") or item.get("image_urls")):
                return item
            video = page.locator("video").first
            if await video.count():
                src = await video.get_attribute("src")
                if src:
                    result = {
                        "aweme_id": str(aweme_id),
                        "description": "",
                        "create_time": None,
                        "video_url": src,
                        "cover_url": await video.get_attribute("poster"),
                        "share_url": page.url,
                        "raw": {},
                    }
                    if media.get("headers"):
                        result["raw"]["_network_headers"] = media["headers"]
                    return result
            author_link = page.locator('a[href*="/user/"]').first
            if await author_link.count():
                href = await author_link.get_attribute("href")
                match = re.search(r"/user/([^/?]+)", href or "")
                if match:
                    return {
                        "aweme_id": str(aweme_id),
                        "description": "",
                        "create_time": None,
                        "video_url": None,
                        "cover_url": None,
                        "share_url": page.url,
                        "nickname": None,
                        "sec_uid": match.group(1),
                        "raw": {"author": {"sec_uid": match.group(1)}},
                    }
            return item
        finally:
            page.remove_listener("response", on_response)
            if response_tasks:
                await asyncio.gather(*list(response_tasks), return_exceptions=True)
            await self.browser.release_page(page, keep_for_verification=True)
