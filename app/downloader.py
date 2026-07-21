from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import aiofiles
import httpx

from app.browser import BrowserManager
from app.config import Settings


INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class DownloadControlRequested(RuntimeError):
    pass


class DownloadPaused(DownloadControlRequested):
    pass


class DownloadCancelled(DownloadControlRequested):
    pass


def safe_name(value: str | None, fallback: str, limit: int = 80) -> str:
    cleaned = INVALID_FILENAME.sub("_", (value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    return (cleaned or fallback)[:limit].rstrip(" .")


def raw_payload(video: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(video.get("raw_json") or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def candidate_video_urls(video: dict[str, Any]) -> list[str]:
    result: list[str] = []
    direct = video.get("video_url")
    if isinstance(direct, str):
        result.append(direct.replace("/playwm/", "/play/"))
    raw = raw_payload(video)

    video_raw = raw.get("video") if isinstance(raw, dict) else None
    if isinstance(video_raw, dict):
        bit_rates = video_raw.get("bit_rate") or video_raw.get("bitRate") or []
        if isinstance(bit_rates, list):
            bit_rates = sorted(
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

        def add_urls(container: Any) -> None:
            if not isinstance(container, dict):
                return
            urls = container.get("url_list") or container.get("urlList") or []
            for url in urls:
                if isinstance(url, str) and url.startswith("http"):
                    result.append(url.replace("/playwm/", "/play/"))

        for entry in bit_rates:
            add_urls(entry.get("play_addr") or entry.get("playAddr"))
        for key in (
            "play_addr_h264", "playAddrH264", "play_addr", "playAddr",
        ):
            add_urls(video_raw.get(key))
    return list(dict.fromkeys(result))


def candidate_image_urls(video: dict[str, Any]) -> list[str]:
    raw = raw_payload(video)
    containers: Any = (
        raw.get("images")
        or raw.get("image_list")
        or raw.get("imageList")
        or raw.get("original_images")
    )
    image_post = raw.get("image_post_info") or raw.get("imagePostInfo")
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


def image_extension(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".avif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".webp"


class VideoDownloader:
    def __init__(self, browser: BrowserManager, settings: Settings):
        self.browser = browser
        self.settings = settings

    def cleanup_stale_temp_files(self, *, minimum_age_seconds: int = 3600) -> int:
        root = self.settings.download_dir.resolve()
        now = time.time()
        removed = 0
        for path in root.rglob("*.part*"):
            try:
                resolved = path.resolve()
                resolved.relative_to(root)
                if not resolved.is_file() or now - resolved.stat().st_mtime < minimum_age_seconds:
                    continue
                if resolved.name.endswith(".part.url"):
                    paired = resolved.with_name(resolved.name.removesuffix(".url"))
                    invalid = not paired.is_file()
                elif resolved.name.endswith(".part"):
                    source = resolved.with_name(resolved.name + ".url")
                    destination = resolved.with_name(resolved.name.removesuffix(".part"))
                    invalid = destination.is_file() or not source.is_file()
                else:
                    invalid = True
                if invalid:
                    resolved.unlink(missing_ok=True)
                    removed += 1
            except (OSError, ValueError):
                continue
        return removed

    async def _download_url(
        self,
        url: str,
        destination: Path,
        referer: str,
        replay_headers: dict[str, str] | None = None,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> int:
        cookies = await self.browser.cookie_dict()
        user_agent = await self.browser.user_agent()
        headers = {
            "User-Agent": user_agent,
            "Referer": referer or "https://www.douyin.com/",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if replay_headers:
            headers.update(
                {
                    key: value
                    for key, value in replay_headers.items()
                    if not key.startswith(":") and key.lower() not in {
                        "cookie", "host", "content-length", "connection", "accept-encoding",
                        "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
                    }
                }
            )
            headers["Referer"] = referer or headers.get("Referer", "https://www.douyin.com/")
            headers["Accept"] = "*/*"
        last_error: Exception | None = None
        temp_path = destination.with_suffix(destination.suffix + ".part")
        source_path = destination.with_suffix(destination.suffix + ".part.url")
        if temp_path.exists():
            try:
                previous_url = source_path.read_text(encoding="utf-8")
            except OSError:
                previous_url = ""
            if previous_url != url:
                temp_path.unlink(missing_ok=True)
                source_path.unlink(missing_ok=True)
        if not temp_path.exists():
            source_path.write_text(url, encoding="utf-8")
        # 短暂网络或限流错误按项目约定立即重试，不人为等待。
        for attempt in range(3):
            try:
                if not source_path.exists():
                    source_path.write_text(url, encoding="utf-8")
                existing_size = temp_path.stat().st_size if temp_path.exists() else 0
                request_headers = dict(headers)
                if existing_size:
                    request_headers["Range"] = f"bytes={existing_size}-"
                async with httpx.AsyncClient(
                    cookies=cookies,
                    headers=request_headers,
                    follow_redirects=True,
                    timeout=httpx.Timeout(self.settings.request_timeout_seconds),
                ) as client:
                    async with client.stream("GET", url) as response:
                        if response.status_code == 416 and existing_size:
                            content_range = response.headers.get("content-range", "")
                            match = re.search(r"\*/(\d+)", content_range)
                            remote_size = int(match.group(1)) if match else None
                            if remote_size == existing_size and existing_size > 1024:
                                os.replace(temp_path, destination)
                                source_path.unlink(missing_ok=True)
                                return existing_size
                            temp_path.unlink(missing_ok=True)
                            source_path.unlink(missing_ok=True)
                            if attempt < 2:
                                continue
                        if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
                            raise httpx.HTTPStatusError(
                                f"temporary status {response.status_code}",
                                request=response.request,
                                response=response,
                            )
                        response.raise_for_status()
                        content_type = response.headers.get("content-type", "").lower()
                        if "text/html" in content_type or "application/json" in content_type:
                            raise RuntimeError(f"媒体地址返回了 {content_type}，可能已失效")
                        append = response.status_code == 206 and existing_size > 0
                        total = existing_size if append else 0
                        length_header = response.headers.get("content-length")
                        response_length = (
                            int(length_header) if length_header and length_header.isdigit() else None
                        )
                        expected = total + response_length if response_length is not None else None
                        if progress:
                            progress(total, expected)
                        async with aiofiles.open(temp_path, "ab" if append else "wb") as file:
                            async for chunk in response.aiter_bytes(1024 * 1024):
                                await file.write(chunk)
                                total += len(chunk)
                                if progress:
                                    progress(total, expected)
                        if total < 1024:
                            temp_path.unlink(missing_ok=True)
                            source_path.unlink(missing_ok=True)
                            raise RuntimeError("下载内容过小，不像有效媒体")
                        os.replace(temp_path, destination)
                        source_path.unlink(missing_ok=True)
                        return total
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if attempt < 2:
                    continue
                raise
        raise RuntimeError(str(last_error or "下载失败"))

    async def _download_cover(self, url: str, destination: Path, referer: str) -> None:
        try:
            await self._download_url(url, destination, referer)
        except Exception:
            destination.unlink(missing_ok=True)

    async def download(
        self,
        creator: dict[str, Any],
        video: dict[str, Any],
        progress: Callable[[int, int | None], None] | None = None,
    ) -> dict[str, Any]:
        creator_dir = self.settings.download_dir / safe_name(
            f"{creator.get('nickname') or '未知用户'}_{creator.get('sec_uid') or creator['id']}",
            f"creator_{creator['id']}",
            120,
        )
        video_dir = creator_dir / "videos"
        cover_dir = creator_dir / "covers"
        note_dir = creator_dir / "notes"
        video_dir.mkdir(parents=True, exist_ok=True)
        cover_dir.mkdir(parents=True, exist_ok=True)
        note_dir.mkdir(parents=True, exist_ok=True)

        timestamp = video.get("create_time")
        date_text = datetime.fromtimestamp(timestamp).strftime("%Y%m%d_%H%M%S") if timestamp else "未知时间"
        base = safe_name(
            f"{video['aweme_id']}_{date_text}_{video.get('description') or '无标题'}",
            str(video["aweme_id"]),
            160,
        )
        if video.get("content_type") == "images":
            image_urls = candidate_image_urls(video)
            if not image_urls:
                raise RuntimeError("图文/日常没有可用的无水印原图地址")
            destination_dir = note_dir / base
            destination_dir.mkdir(parents=True, exist_ok=True)
            payload = raw_payload(video)
            replay_headers = payload.get("_network_headers")
            if not isinstance(replay_headers, dict):
                replay_headers = None
            paths = [
                destination_dir / f"{index:03d}{image_extension(url)}"
                for index, url in enumerate(image_urls, start=1)
            ]
            current_sizes = {
                index: path.stat().st_size if path.exists() and path.stat().st_size > 1024 else 0
                for index, path in enumerate(paths)
            }
            semaphore = asyncio.Semaphore(min(4, max(1, self.settings.download_concurrency * 2)))

            async def download_image(index: int, url: str, destination: Path) -> int:
                if current_sizes[index]:
                    return current_sizes[index]
                async with semaphore:
                    def image_progress(current: int, _total: int | None) -> None:
                        current_sizes[index] = current
                        if progress:
                            progress(sum(current_sizes.values()), None)

                    size = await self._download_url(
                        url,
                        destination,
                        video.get("share_url") or creator["profile_url"],
                        replay_headers=replay_headers,
                        progress=image_progress,
                    )
                    current_sizes[index] = size
                    return size

            sizes = await asyncio.gather(
                *(
                    download_image(index, url, destination)
                    for index, (url, destination) in enumerate(zip(image_urls, paths))
                )
            )
            total_size = sum(sizes)
            if progress:
                progress(total_size, total_size)

            # Preserve the soundtrack file produced by older builds, but move it out of the
            # videos directory and give it an honest extension after the images succeed.
            legacy_value = video.get("file_path")
            if isinstance(legacy_value, str) and legacy_value.lower().endswith(".mp4"):
                legacy = Path(legacy_value).resolve()
                download_root = self.settings.download_dir.resolve()
                if download_root in legacy.parents and legacy.is_file():
                    soundtrack = destination_dir / "soundtrack_from_legacy_download.mp3"
                    if not soundtrack.exists():
                        os.replace(legacy, soundtrack)

            metadata = {
                "aweme_id": video["aweme_id"],
                "description": video.get("description"),
                "create_time": video.get("create_time"),
                "share_url": video.get("share_url"),
                "content_type": "images",
                "asset_count": len(paths),
                "file_path": str(destination_dir),
                "downloaded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            async with aiofiles.open(creator_dir / "metadata.jsonl", "a", encoding="utf-8") as file:
                await file.write(json.dumps(metadata, ensure_ascii=False) + "\n")
            return {
                "file_path": str(destination_dir),
                "cover_path": str(paths[0]) if paths else None,
                "file_size": total_size,
            }

        destination = video_dir / f"{base}.mp4"
        cover_path = cover_dir / f"{base}.jpg"
        if destination.exists() and destination.stat().st_size > 1024:
            size = destination.stat().st_size
        else:
            errors: list[str] = []
            size = 0
            payload = raw_payload(video)
            replay_headers = payload.get("_network_headers")
            if not isinstance(replay_headers, dict):
                replay_headers = None
            for url in candidate_video_urls(video):
                try:
                    size = await self._download_url(
                        url,
                        destination,
                        video.get("share_url") or creator["profile_url"],
                        replay_headers=replay_headers,
                        progress=progress,
                    )
                    break
                except DownloadControlRequested:
                    raise
                except Exception as exc:
                    errors.append(str(exc))
            if not size:
                raise RuntimeError("；".join(errors[-3:]) or "没有可用的视频地址")

        if video.get("cover_url") and not cover_path.exists():
            await self._download_cover(
                video["cover_url"], cover_path, video.get("share_url") or creator["profile_url"]
            )

        metadata = {
            "aweme_id": video["aweme_id"],
            "description": video.get("description"),
            "create_time": video.get("create_time"),
            "share_url": video.get("share_url"),
            "file_path": str(destination),
            "downloaded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        async with aiofiles.open(creator_dir / "metadata.jsonl", "a", encoding="utf-8") as file:
            await file.write(json.dumps(metadata, ensure_ascii=False) + "\n")
        return {
            "file_path": str(destination),
            "cover_path": str(cover_path) if cover_path.exists() else None,
            "file_size": size,
        }
