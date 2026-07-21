from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import httpx


class CoverCache:
    def __init__(self, max_concurrency: int = 2) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._locks: dict[int, asyncio.Lock] = {}

    async def ensure_local(self, video: dict[str, Any], download_dir: Path) -> Path:
        video_id = int(video["id"])
        lock = self._locks.setdefault(video_id, asyncio.Lock())
        async with lock:
            existing = video.get("cover_path")
            if existing:
                path = Path(str(existing))
                if not path.is_absolute():
                    path = download_dir / path
                if path.is_file():
                    return path.resolve()

            url = str(video.get("cover_url") or "").strip()
            if not url:
                raise FileNotFoundError("作品没有可用封面地址")

            cache_dir = download_dir / ".cache" / "covers"
            cache_dir.mkdir(parents=True, exist_ok=True)
            destination = cache_dir / f"{video_id}_{video.get('aweme_id') or video_id}.jpg"
            if destination.is_file():
                return destination.resolve()

            temp_path = destination.with_suffix(".jpg.part")
            temp_path.unlink(missing_ok=True)
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
                "Referer": str(video.get("share_url") or "https://www.douyin.com/"),
            }
            try:
                async with self._semaphore:
                    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
                        async with client.stream("GET", url, headers=headers) as response:
                            response.raise_for_status()
                            content_type = response.headers.get("content-type", "").lower()
                            if content_type and not content_type.startswith("image/"):
                                raise ValueError("远程封面返回的不是图片")
                            with temp_path.open("wb") as target:
                                async for chunk in response.aiter_bytes():
                                    target.write(chunk)
                if not temp_path.is_file() or temp_path.stat().st_size == 0:
                    raise ValueError("远程封面内容为空")
                os.replace(temp_path, destination)
                return destination.resolve()
            except Exception:
                temp_path.unlink(missing_ok=True)
                raise
