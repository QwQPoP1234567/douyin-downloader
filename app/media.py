from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi.responses import FileResponse


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".heic"}


class MediaPathError(ValueError):
    pass


def resolve_download_path(
    download_root: Path, value: str | Path, *, expect_directory: bool = False
) -> Path:
    root = download_root.resolve()
    path = Path(value).resolve()
    if path == root or root not in path.parents:
        raise MediaPathError("媒体路径不在下载目录中")
    if not path.exists():
        raise FileNotFoundError(path)
    if expect_directory and not path.is_dir():
        raise MediaPathError("媒体路径不是图文目录")
    if not expect_directory and not path.is_file():
        raise MediaPathError("媒体路径不是文件")
    return path


def list_local_images(download_root: Path, directory: str | Path) -> list[Path]:
    path = resolve_download_path(download_root, directory, expect_directory=True)
    return sorted(
        item
        for item in path.iterdir()
        if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES
    )


def inline_file_response(path: Path, *, default_media_type: str) -> FileResponse:
    media_type = mimetypes.guess_type(path.name)[0] or default_media_type
    response = FileResponse(
        path,
        media_type=media_type,
        content_disposition_type="inline",
    )
    response.headers.setdefault("Accept-Ranges", "bytes")
    return response


def detect_image_media_type(path: Path) -> str:
    with path.open("rb") as file:
        header = file.read(16)
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    if len(header) >= 12 and header[4:8] == b"ftyp" and header[8:12] in {b"avif", b"avis"}:
        return "image/avif"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def inline_image_response(path: Path) -> FileResponse:
    response = FileResponse(
        path,
        media_type=detect_image_media_type(path),
        content_disposition_type="inline",
    )
    response.headers.setdefault("Accept-Ranges", "bytes")
    return response
