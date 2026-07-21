import asyncio
from pathlib import Path

import httpx

from app.cover_cache import CoverCache


def test_cover_cache_downloads_once_and_reuses_local_file(tmp_path: Path, monkeypatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"jpeg-data")

    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    cache = CoverCache()
    video = {"id": 7, "aweme_id": "abc", "cover_url": "https://example.test/cover.jpg"}

    first = asyncio.run(cache.ensure_local(video, tmp_path))
    second = asyncio.run(cache.ensure_local(video, tmp_path))

    assert first == second
    assert first.read_bytes() == b"jpeg-data"
    assert calls == 1


def test_cover_cache_rejects_non_image_response(tmp_path: Path, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"blocked")

    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    cache = CoverCache()
    video = {"id": 8, "aweme_id": "blocked", "cover_url": "https://example.test/cover.jpg"}

    try:
        asyncio.run(cache.ensure_local(video, tmp_path))
    except ValueError as exc:
        assert "不是图片" in str(exc)
    else:
        raise AssertionError("expected non-image response to be rejected")
    assert not list(tmp_path.rglob("*.part"))
