from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.media import MediaPathError, detect_image_media_type, inline_file_response, list_local_images, resolve_download_path


def test_media_paths_must_stay_inside_download_root(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    inside = root / "creator" / "video.mp4"
    inside.parent.mkdir()
    inside.write_bytes(b"0123456789")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")

    assert resolve_download_path(root, inside) == inside.resolve()
    with pytest.raises(MediaPathError):
        resolve_download_path(root, outside)
    with pytest.raises(MediaPathError):
        resolve_download_path(root, root)


def test_local_images_are_filtered_and_sorted(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    directory = root / "creator" / "notes" / "post"
    directory.mkdir(parents=True)
    (directory / "002.webp").write_bytes(b"image")
    (directory / "001.jpg").write_bytes(b"image")
    (directory / "metadata.json").write_text("{}", encoding="utf-8")

    assert [path.name for path in list_local_images(root, directory)] == [
        "001.jpg",
        "002.webp",
    ]


def test_file_response_supports_http_range(tmp_path: Path) -> None:
    media = tmp_path / "video.mp4"
    media.write_bytes(b"0123456789")
    test_app = FastAPI()

    @test_app.get("/media")
    async def serve_media():
        return inline_file_response(media, default_media_type="video/mp4")

    response = TestClient(test_app).get("/media", headers={"Range": "bytes=2-5"})

    assert response.status_code == 206
    assert response.content == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-type"].startswith("video/mp4")


def test_detect_image_media_type_uses_file_signature(tmp_path: Path) -> None:
    disguised_webp = tmp_path / "cover.jpg"
    disguised_webp.write_bytes(b"RIFF\x08\x00\x00\x00WEBPdata")

    assert detect_image_media_type(disguised_webp) == "image/webp"
